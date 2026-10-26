#!/usr/bin/env python3
"""Resolve which ATMs the EJ journal pull should fetch, from data.

Before this existed the pull list was a hardcoded line in
scripts/pull_ej_logs.sh, so every new ATM meant editing a script — the exact
manual step this project is trying to remove.

Sources, first hit wins:

  1. $EJ_ATMS              explicit override, space/newline separated
                           `ATM-ID:IP:HOSTNAME` (unchanged escape hatch)
  2. PostgreSQL            the portal DB, read the way every script in this repo
                           reads it (docker exec zabbix-db psql). Source of
                           truth — this is what production will use.
  3. config/postgres/atm_locations.csv
                           nightly snapshot written by scripts/backup_db.sh.
                           Keeps the pull working on a host that cannot reach
                           the DB container, and degrades to "yesterday" rather
                           than "nothing".

An ATM is only a pull target when all of these hold:
  vendor = the requested vendor (NCR or GRG),
  status = active,
  ip_address present (needed for the Zabbix agent call),
  ej_hostname present (it names the local evidence file and maps it back to
  the ATM: <date>_<HOSTNAME>_EJDATA.log). Skip loudly rather than guessing.

NCR journals live in E:\\EJLogs and are named <date>_<HOST>_EJDATA.log on the
box. GRG's application journal is D:\\Program Files\\DTATMW\\Bin\\ATMAPP\\Log\\
EJ<yymmdd>.log; the puller renames it to the same canonical local form so the
existing ingest/metadata path does not need a second dialect.

Output: one `ATM-ID:IP:HOSTNAME` line per target on stdout, so the caller can
`while IFS=: read -r ...`. Diagnostics go to stderr, keeping stdout parseable.

Usage:
  scripts/ej_pull_targets.py                    # NCR targets (default)
  scripts/ej_pull_targets.py --vendor GRG        # GRG targets
  scripts/ej_pull_targets.py --explain           # print skip reasons
Stdlib only, like every other helper in scripts/.
"""

import csv
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO, 'config', 'postgres', 'atm_locations.csv')
DB_CONTAINER = os.environ.get('EJ_DB_CONTAINER', 'zabbix-db')
# Cron runs with a minimal PATH, and docker is not always in it. An absolute
# path here removes the last way this resolver can silently degrade.
DOCKER_DEFAULT = 'docker'


def _docker_bin(explicit=None):
    """Read at call time, not import time, so the env var always wins."""
    return explicit or os.environ.get('EJ_DOCKER_BIN') or DOCKER_DEFAULT
DB_USER = os.environ.get('DB_USER', 'zabbix')

# Which database holds the portal tables. This resolver runs EVERY MINUTE from
# cron, and the cron line exports ZBX_URL/ZBX_USER/ZBX_PASS but not DB_NAME - so
# the old hardcoded default of 'zabbix' meant the EJ pipeline resolved its
# target ATMs from the pre-split database, which stopped moving the moment the
# portal switched.
#
# Nothing about that fails. The read succeeds, it just returns the old rows. So
# an ATM registered in the portal would never be pulled for, and the only
# symptom would be an ATM quietly having no journals. It worked on UAT only
# because atm_locations happened not to have changed since the copy.
#
# portal_schema_version is a portal-only marker created by txn_sources.py, and
# it travels with the copy - which is what makes it usable as the test. An
# explicit DB_NAME still wins, for an operator who knows better.
_MARKER = "SELECT to_regclass('public.portal_schema_version') IS NOT NULL;"


def detect_portal_db(docker_bin=None):
    """Return the database holding the portal tables, or None."""
    for candidate in ('portal', 'zabbix'):
        cmd = [_docker_bin(explicit=docker_bin), 'exec', DB_CONTAINER, 'psql',
               '-U', DB_USER, '-d', candidate, '-tAc', _MARKER]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode == 0 and (out.stdout or '').strip() == 't':
            return candidate
    return None


def _resolve_db_name(docker_bin=None):
    """Read at call time, not import time - the same rule _docker_bin follows.

    Resolving this at import meant an operator exporting DB_NAME before running
    the script was ignored, and it made the DB path untestable. Both were
    discovered by existing tests failing after this change, which is the tests
    doing their job.
    """
    explicit = os.environ.get('DB_NAME')
    if explicit:
        return explicit
    return detect_portal_db(docker_bin=docker_bin)

# Same predicate as the DB query below — kept identical on purpose so the CSV
# fallback and the live DB can never disagree about who is eligible.
JOURNAL_VENDORS = ('NCR', 'GRG')
NCR_ACTIVE = ('NCR', 'active')


def normalize(atm_id, ip, hostname):
    """Return a clean target tuple, or None when a field is unusable.

    Hostnames are upper-cased and stripped: NCR journal filenames use the
    switch-reported host name, which is case-insensitive in practice but is
    not guaranteed to match what the box reports.
    """
    atm_id = (atm_id or '').strip()
    ip = (ip or '').strip()
    hostname = (hostname or '').strip().upper()
    if not atm_id or not ip or not hostname:
        return None
    if any(c.isspace() or c == ':' for c in atm_id + ip + hostname):
        return None
    return atm_id, ip, hostname


def eligible(row, vendor='NCR'):
    """Pure predicate — unit-tested; mirrors the SQL WHERE clause."""
    want = (vendor or 'NCR').strip().upper()
    if want not in JOURNAL_VENDORS:
        return False, f'unsupported journal vendor {want!r}'
    row_vendor = (row.get('vendor') or '').strip().upper()
    status = (row.get('status') or 'active').strip().lower()
    if row_vendor != want:
        return False, f'vendor {row_vendor or "?"} is not a {want} journal target'
    if status != NCR_ACTIVE[1]:
        return False, 'ATM is not active'
    if not (row.get('ip_address') or '').strip():
        return False, 'no IP address recorded'
    if not (row.get('ej_hostname') or '').strip():
        return False, 'no EJ hostname recorded (used to name <date>_<HOST>_EJDATA.log)'
    return True, ''


def targets_from_rows(rows, already_filtered=False, vendor='NCR'):
    """rows: iterable of dicts. Returns (targets, skips) — skips is diagnostic.

    already_filtered=True when the source already applied the eligibility rule
    in SQL (the DB query mirrors eligible()); only shape validation is left.
    """
    targets, skips = [], []
    for row in rows:
        if not already_filtered:
            ok, why = eligible(row, vendor=vendor)
            if not ok:
                if (row.get('atm_id') or '').strip():
                    skips.append((row['atm_id'].strip(), why))
                continue
        t = normalize(row.get('atm_id'), row.get('ip_address'), row.get('ej_hostname'))
        if t is None:
            skips.append((row.get('atm_id', '').strip(), 'fields contain characters the pull cannot use'))
            continue
        if t not in targets:
            targets.append(t)
    return targets, skips


# ── source 1: explicit override ────────────────────────────────────────────
def from_override(raw):
    targets = []
    for chunk in (raw or '').replace('\n', ' ').split():
        parts = chunk.split(':')
        if len(parts) != 3:
            sys.stderr.write(f"ej_pull_targets: ignoring malformed EJ_ATMS entry {chunk!r}\n")
            continue
        t = normalize(*parts)
        if t:
            targets.append(t)
        else:
            sys.stderr.write(f"ej_pull_targets: ignoring incomplete EJ_ATMS entry {chunk!r}\n")
    return targets, []


# ── source 2: the portal database ─────────────────────────────────────────
def db_query(vendor='NCR'):
    """Fleet query for one journal dialect; vendor is whitelisted, not interpolated."""
    vendor = (vendor or 'NCR').strip().upper()
    if vendor not in JOURNAL_VENDORS:
        raise ValueError(f'unsupported journal vendor {vendor!r}')
    return (
        "SELECT atm_id, ip_address, ej_hostname FROM atm_locations "
        f"WHERE UPPER(vendor) = '{vendor}' AND LOWER(COALESCE(status,'active')) = 'active' "
        "AND COALESCE(ip_address,'') <> '' AND COALESCE(ej_hostname,'') <> '' "
        "ORDER BY atm_id"
    )


def from_db(docker_bin=None, vendor='NCR'):
    """Read targets via the repo's established DB access pattern.

    Returns None (not []) when the DB could not be read — the caller must then
    fall back rather than conclude there are no ATMs to pull.
    """
    db_name = _resolve_db_name(docker_bin=docker_bin)
    if not db_name:
        # No marker found in either database, and no explicit override. Say so
        # plainly rather than reading a database at random: the caller falls
        # back to the CSV snapshot, which is written nightly from the portal and
        # is therefore safe to use, if a little stale.
        sys.stderr.write('ej_pull_targets: could not determine which database '
                         'holds the portal tables; using the CSV snapshot\n')
        return None
    cmd = [_docker_bin(explicit=docker_bin), 'exec', DB_CONTAINER, 'psql',
           '-U', DB_USER, '-d', db_name, '-tAF|', '-c', db_query(vendor)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f'ej_pull_targets: DB read failed ({e})\n')
        return None
    if out.returncode != 0:
        first = ((out.stderr or '').strip().splitlines() or ['no detail'])[0]
        sys.stderr.write(f'ej_pull_targets: DB read failed: {first}\n')
        return None
    rows = []
    for line in out.stdout.splitlines():
        parts = line.strip().split('|')
        if len(parts) != 3:
            continue
        rows.append({'atm_id': parts[0], 'ip_address': parts[1], 'ej_hostname': parts[2]})
    # The WHERE clause above already applied eligible() in SQL.
    return targets_from_rows(rows, already_filtered=True, vendor=vendor)


# ── source 3: nightly CSV snapshot ────────────────────────────────────────
def from_csv(path=CSV_PATH, vendor='NCR'):
    if not os.path.exists(path):
        return None
    try:
        with open(path, newline='', encoding='utf-8-sig') as fh:
            rows = list(csv.DictReader(fh))
    except OSError as e:
        sys.stderr.write(f'ej_pull_targets: CSV read failed ({e})\n')
        return None
    if not rows:
        return None
    return targets_from_rows(rows, vendor=vendor)


def resolve(explain=False, csv_path=CSV_PATH, docker_bin=None, vendor='NCR'):
    """Returns (targets, source_name, skips). Never raises."""
    vendor = (vendor or 'NCR').strip().upper()
    if vendor not in JOURNAL_VENDORS:
        return [], 'none', []
    db_bin = _docker_bin(explicit=docker_bin)
    override = os.environ.get('EJ_ATMS')
    if override and override.strip():
        targets, _ = from_override(override)
        return targets, 'EJ_ATMS override', []

    # Name the database in the source label. "1 target(s) from database" tells
    # an operator that the DB was used and nothing about which one - so after the
    # split, reading the pre-split database looked identical to reading the
    # right one. The whole point of detecting it is lost if the log cannot show
    # the result.
    for name, getter in (
            ('database ' + (os.environ.get('DB_NAME') or _resolve_db_name(db_bin) or '?'),
             lambda: from_db(db_bin, vendor=vendor)),
                         ('CSV snapshot', lambda: from_csv(csv_path, vendor=vendor))):
        result = getter()
        if result is None:
            continue
        targets, skips = result
        if targets:
            if explain and skips:
                for atm_id, why in skips:
                    sys.stderr.write(f'  skipped {atm_id}: {why}\n')
            return targets, name, skips
        sys.stderr.write(f'ej_pull_targets: {name} yielded no eligible {vendor} ATMs'
                         + (' (snapshot predates the ej_hostname column — run '
                            'scripts/migrate_ej_hostname.sql)' if name == 'CSV snapshot' else '')
                         + '\n')
    return [], 'none', []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    explain = '--explain' in argv
    vendor = 'NCR'
    for i, arg in enumerate(argv):
        if arg == '--vendor' and i + 1 < len(argv):
            vendor = argv[i + 1].upper()
        elif arg.startswith('--vendor='):
            vendor = arg.split('=', 1)[1].upper()
    targets, source, skips = resolve(explain=explain, vendor=vendor)
    sys.stderr.write(f'ej_pull_targets: {len(targets)} target(s) from {source}\n')
    for atm_id, ip, hostname in targets:
        print(f'{atm_id}:{ip}:{hostname}')
    return 0


if __name__ == '__main__':
    sys.exit(main())