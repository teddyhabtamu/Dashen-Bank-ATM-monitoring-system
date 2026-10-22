#!/usr/bin/env python3
"""Load parsed EJ journals into the portal's quarantined staging tables.

This is deliberately NOT the path into atm_transactions. The parser has been
validated against real TESTMACHINE journals, but the EJ feed still has
unresolved gaps (no-receipt test sessions, a missing cash-dispense sample and
no real GRG journal). Rows here are visible for reconciliation and never feed
KPIs, cash forecasts or anomalies.

Usage on the UAT host:

    python3 scripts/ingest_ej_shadow.py                 # dry run
    python3 scripts/ingest_ej_shadow.py --commit        # write staging

The database is read through `docker exec zabbix-db psql`, matching
scripts/ej_pull_targets.py. This script is stdlib-only: the UAT host has no
psycopg2 and no venv.

Idempotency:
  * a parse run is unique on (file_path, file_sha256)
  * a transaction is unique on its parser transaction_key
Re-running the same journals updates the same rows instead of duplicating them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from ej_journal import PARSER_VERSION, parse_journal_file  # noqa: E402

DB_CONTAINER = os.environ.get('EJ_DB_CONTAINER', 'zabbix-db')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DOCKER_DEFAULT = 'docker'

PARSE_COLUMNS = (
    'file_path', 'file_sha256', 'vendor', 'encoding',
    'sessions', 'completed', 'approved', 'declined', 'unknown',
    'incomplete', 'amount_total', 'dispensed_total', 'warnings',
    'parser_version', 'status', 'error',
)
TXN_COLUMNS = (
    'transaction_key', 'parse_file', 'parse_sha',
    'vendor', 'atm_id', 'terminal_id', 'branch', 'recorded_at',
    'txn_type', 'status', 'amount', 'currency', 'dispensed_amount',
    'card_masked', 'auth_code', 'error_code', 'seq_number',
    'warnings', 'confidence', 'parser_version', 'raw_ref', 'source_file',
)


def _docker_bin(explicit=None):
    return explicit or os.environ.get('EJ_DOCKER_BIN') or DOCKER_DEFAULT


def _psql(docker_bin, db_name, sql, *, query=False, check=True):
    """Run SQL through the portal database container.

    Returns CompletedProcess. psql reads stdin, which is what lets a COPY
    stream and its data travel in one round trip.
    """
    cmd = [_docker_bin, 'exec', '-i', DB_CONTAINER, 'psql',
           '-U', DB_USER, '-d', db_name, '-v', 'ON_ERROR_STOP=1']
    if query:
        cmd.extend(['-tAF|'])
    else:
        cmd.append('-q')
    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or 'psql failed').strip()
        raise RuntimeError(detail.splitlines()[0])
    return proc


def _resolve_db_name(explicit=None, docker_bin=None):
    """Find the database that holds the portal schema, never guess silently.

    DB_NAME wins. Otherwise use the portal_schema_version marker, exactly as
    scripts/ej_pull_targets.py does.
    """
    if explicit:
        return explicit
    marker = "SELECT to_regclass('public.portal_schema_version') IS NOT NULL;"
    for candidate in ('portal', 'zabbix'):
        try:
            proc = _psql(_docker_bin(docker_bin), candidate, marker,
                         query=True, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0 and (proc.stdout or '').strip() == 't':
            return candidate
    return None


def _read_terminal_map(db_name, docker_bin=None):
    """terminal_id -> atm_id from the registry. Empty on failure."""
    if not db_name:
        return {}
    sql = ("SELECT terminal_id, atm_id FROM atm_locations "
           "WHERE COALESCE(terminal_id, '') <> '' ORDER BY terminal_id;")
    proc = _psql(_docker_bin(docker_bin), db_name, sql, query=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or 'terminal map query failed').strip())
    mapping = {}
    for line in (proc.stdout or '').splitlines():
        terminal, _, atm_id = line.partition('|')
        if terminal.strip() and atm_id.strip():
            mapping[terminal.strip()] = atm_id.strip()
    return mapping


def _load_json_map(path):
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('terminal map must be a JSON object')
    return {str(k): str(v) for k, v in data.items() if v}


def _csv_rows(columns, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            if value is None:
                values.append('')
            elif isinstance(value, (dict, list)):
                values.append(json.dumps(value, sort_keys=True, ensure_ascii=False))
            elif isinstance(value, datetime):
                values.append(value.isoformat(sep=' '))
            elif isinstance(value, Decimal):
                values.append(format(value, 'f'))
            else:
                values.append(str(value))
        writer.writerow(values)
    return buf.getvalue()


def _build_sql(parse_runs, transactions):
    """Build one psql script: temp COPY tables then idempotent upserts."""
    out = io.StringIO()
    out.write('BEGIN;\n')

    out.write(
        'CREATE TEMP TABLE _ej_parse_runs '
        '(LIKE public.ej_parse_runs INCLUDING DEFAULTS) ON COMMIT DROP;\n')
    out.write('COPY _ej_parse_runs (%s) FROM STDIN WITH (FORMAT csv);\n'
              % ', '.join(PARSE_COLUMNS))
    out.write(_csv_rows(PARSE_COLUMNS, parse_runs))
    out.write('\\.\n')
    out.write("""
INSERT INTO public.ej_parse_runs (
    file_path, file_sha256, vendor, encoding, sessions, completed, approved,
    declined, unknown, incomplete, amount_total, dispensed_total, warnings,
    parser_version, status, error
)
SELECT file_path, file_sha256, vendor, NULLIF(encoding, ''),
       sessions::integer, completed::integer, approved::integer,
       declined::integer, unknown::integer, incomplete::integer,
       NULLIF(amount_total, '')::numeric,
       NULLIF(dispensed_total, '')::numeric,
       COALESCE(NULLIF(warnings, '')::jsonb, '{}'::jsonb),
       parser_version, status, NULLIF(error, '')
  FROM _ej_parse_runs
ON CONFLICT (file_path, file_sha256) DO UPDATE SET
    vendor = EXCLUDED.vendor,
    encoding = EXCLUDED.encoding,
    sessions = EXCLUDED.sessions,
    completed = EXCLUDED.completed,
    approved = EXCLUDED.approved,
    declined = EXCLUDED.declined,
    unknown = EXCLUDED.unknown,
    incomplete = EXCLUDED.incomplete,
    amount_total = EXCLUDED.amount_total,
    dispensed_total = EXCLUDED.dispensed_total,
    warnings = EXCLUDED.warnings,
    parser_version = EXCLUDED.parser_version,
    parsed_at = now(),
    status = EXCLUDED.status,
    error = EXCLUDED.error;
""")

    if transactions:
        out.write(
            'CREATE TEMP TABLE _ej_txns ('
            + ', '.join('%s text' % c for c in TXN_COLUMNS)
            + ') ON COMMIT DROP;\n')
        out.write('COPY _ej_txns (%s) FROM STDIN WITH (FORMAT csv);\n'
                  % ', '.join(TXN_COLUMNS))
        out.write(_csv_rows(TXN_COLUMNS, transactions))
        out.write('\\.\n')
        out.write("""
INSERT INTO public.ej_transactions (
    transaction_key, parse_run_id, vendor, atm_id, terminal_id, branch,
    recorded_at, txn_type, status, amount, currency, dispensed_amount,
    card_masked, auth_code, error_code, seq_number, warnings, confidence,
    parser_version, raw_ref, source_file
)
SELECT t.transaction_key, r.id, t.vendor, t.atm_id, t.terminal_id, t.branch,
       t.recorded_at::timestamp, t.txn_type, t.status,
       NULLIF(t.amount, '')::numeric, NULLIF(t.currency, ''),
       NULLIF(t.dispensed_amount, '')::numeric,
       t.card_masked, t.auth_code, t.error_code, t.seq_number,
       COALESCE(NULLIF(t.warnings, '')::jsonb, '[]'::jsonb),
       t.confidence, t.parser_version, t.raw_ref, t.source_file
  FROM _ej_txns t
  JOIN public.ej_parse_runs r
    ON r.file_path = t.parse_file
   AND r.file_sha256 = t.parse_sha
ON CONFLICT (transaction_key) DO UPDATE SET
    parse_run_id = EXCLUDED.parse_run_id,
    vendor = EXCLUDED.vendor,
    atm_id = EXCLUDED.atm_id,
    terminal_id = EXCLUDED.terminal_id,
    branch = EXCLUDED.branch,
    recorded_at = EXCLUDED.recorded_at,
    txn_type = EXCLUDED.txn_type,
    status = EXCLUDED.status,
    amount = EXCLUDED.amount,
    currency = EXCLUDED.currency,
    dispensed_amount = EXCLUDED.dispensed_amount,
    card_masked = EXCLUDED.card_masked,
    auth_code = EXCLUDED.auth_code,
    error_code = EXCLUDED.error_code,
    seq_number = EXCLUDED.seq_number,
    warnings = EXCLUDED.warnings,
    confidence = EXCLUDED.confidence,
    parser_version = EXCLUDED.parser_version,
    raw_ref = EXCLUDED.raw_ref,
    source_file = EXCLUDED.source_file,
    ingested_at = now();
""")

    out.write('COMMIT;\n')
    return out.getvalue()


def collect(paths, terminal_map):
    """Parse journals and return (parse_runs, transactions, per_file_summary).

    Invalid/unreadable files are reported to stderr and skipped; a journal that
    parses but has no recognised records still gets a parse_run row with its
    counts, so an empty day is evidence, not silence.
    """
    parse_runs, transactions, files, errors = [], [], [], []
    seen_runs, seen_txns, seen_files = set(), set(), set()

    for path in paths:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append(f'{path}: {exc}')
            continue
        digest = hashlib.sha256(raw).hexdigest()
        run_key = (str(path), digest)
        if run_key in seen_runs:
            continue
        seen_runs.add(run_key)
        try:
            result = parse_journal_file(path, terminal_map=terminal_map)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            errors.append(f'{path}: {exc}')
            parse_runs.append({
                'file_path': str(path), 'file_sha256': digest,
                'vendor': 'UNKNOWN', 'encoding': '', 'sessions': 0,
                'completed': 0, 'approved': 0, 'declined': 0, 'unknown': 0,
                'incomplete': 0, 'amount_total': '0', 'dispensed_total': '0',
                'warnings': {}, 'parser_version': PARSER_VERSION,
                'status': 'error', 'error': str(exc),
            })
            continue

        summary = result.summary()
        status = 'ok'
        error = None
        if result.vendor == 'UNKNOWN':
            status = 'error'
            error = 'unrecognised journal format'
        parse_runs.append({
            'file_path': str(path),
            'file_sha256': digest,
            'vendor': result.vendor,
            'encoding': result.encoding,
            'sessions': summary['sessions'],
            'completed': summary['completed'],
            'approved': summary['approved'],
            'declined': summary['declined'],
            'unknown': summary['unknown'],
            'incomplete': summary['incomplete'],
            'amount_total': summary['amount'],
            'dispensed_total': summary['dispensed'],
            'warnings': summary['warnings'],
            'parser_version': PARSER_VERSION,
            'status': status,
            'error': error,
        })

        staged = 0
        for txn in result.transactions:
            if not txn.eligible_for_ingest:
                continue
            row = txn.to_shadow_row()
            if row['transaction_key'] in seen_txns:
                continue
            seen_txns.add(row['transaction_key'])
            row.update({
                'parse_file': str(path),
                'parse_sha': digest,
                'source_file': str(path),
            })
            transactions.append(row)
            staged += 1
        if str(path) not in seen_files:
            seen_files.add(str(path))
            files.append({
                'path': str(path),
                'vendor': result.vendor,
                'sessions': summary['sessions'],
                'completed': summary['completed'],
                'staged': staged,
                'unknown': summary['unknown'],
            })
    return parse_runs, transactions, files, errors


def _print_plan(files, parse_runs, transactions, commit=False):
    for item in files:
        print('{vendor:3} {path}: sessions={sessions} completed={completed} '
              'staged={staged} unknown={unknown}'.format(**item))
    staged = len(transactions)
    label = 'PLAN' if commit else 'DRY RUN'
    print('%s: would stage %d transaction(s) and %d parse run(s)%s.'
          % (label, staged, len(parse_runs),
             '' if commit else '; no database writes'))
    if staged:
        print('%s: %d completed transaction(s) are mapped and eligible; '
              'unknown/no-receipt sessions are not staged.' % (label, staged))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Load parsed EJ journals into the portal staging tables.')
    ap.add_argument('journals', nargs='*', type=Path,
                    help='journal files (default: ej-logs/*_EJDATA.log)')
    ap.add_argument('--commit', action='store_true',
                    help='write to the portal database (default: dry run)')
    ap.add_argument('--terminal-map', type=Path, default=None,
                    help='optional JSON override terminal_id -> atm_id')
    ap.add_argument('--db-name', default=os.environ.get('DB_NAME'),
                    help='portal database name (default: detect)')
    ap.add_argument('--json', action='store_true', help='machine-readable summary')
    args = ap.parse_args(argv)

    paths = args.journals or sorted(
        (ROOT / 'ej-logs').glob('*_EJDATA.log'))
    if not paths:
        print('No journals matched. Run scripts/pull_ej_logs.sh first.',
              file=sys.stderr)
        return 1

    db_name = _resolve_db_name(args.db_name)
    if not db_name:
        print('Could not determine the portal database. Set DB_NAME=portal.',
              file=sys.stderr)
        return 2

    try:
        terminal_map = _read_terminal_map(db_name)
        terminal_map.update(_load_json_map(args.terminal_map))
    except Exception as exc:  # noqa: BLE001
        print('Terminal mapping failed: %s' % exc, file=sys.stderr)
        return 2

    parse_runs, transactions, files, errors = collect(paths, terminal_map)

    if args.json:
        print(json.dumps({
            'files': files,
            'parse_runs': parse_runs,
            'transactions': transactions,
            'errors': errors,
            'would_stage': len(transactions),
        }, indent=2, ensure_ascii=False, default=str))
    else:
        _print_plan(files, parse_runs, transactions, commit=args.commit)
        for error in errors:
            print('ERROR ' + error, file=sys.stderr)

    if not args.commit:
        return 1 if errors else 0

    try:
        sql = _build_sql(parse_runs, transactions)
        proc = _psql(_docker_bin(), db_name, sql)
    except Exception as exc:  # noqa: BLE001
        print('COMMIT FAILED: %s' % exc, file=sys.stderr)
        return 1

    if proc.stdout.strip():
        print(proc.stdout.strip())
    print('COMMITTED: %d transaction(s), %d parse run(s) to %s.'
          % (len(transactions), len(parse_runs), db_name))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
