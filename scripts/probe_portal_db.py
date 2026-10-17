#!/usr/bin/env python3
"""Stage 3: prove the `portal` database can actually serve portal queries.

Nothing is switched. No service is restarted. No env file is edited. This
process opens one connection, runs the queries the portal runs, and exits. If it
prints OK, the copy is not merely row-complete but genuinely usable - the two
are not the same thing, and only the second one justifies Stage 4.

The row counts were already proven equal to the snapshot by
split_portal_db.sh --verify. What that cannot tell you is whether the queries
the application issues actually work against the copy: a missing view, a column
that only exists in one database, a join that no longer resolves. Those fail at
runtime, on a page, during a demonstration.

THE CONNECTION IS READ ONLY. The transaction is opened readonly, so any write
the probe might attempt fails loudly instead of quietly perturbing the copy -
which matters, because a probe that wrote would make the next --verify report a
MISMATCH against the manifest and it would be unclear who caused it.

Usage, from the repo root on the server:

    docker exec -e DB_NAME=portal -i report-portal python3 - < scripts/probe_portal_db.py

The -e override applies to that one process only. Nothing else changes.
"""
import os
import sys

FAILURES = []
NOTES = []


def view_evidence(raw_rows, visible_rows, joined_rows):
    """Can the view and join results actually be believed?

    With the demo window closed, atm_transactions_visible returns 0 rows - and
    so does any join built on it. A view that was broken, mis-joined or missing
    a predicate would return 0 as well, so a zero here is indistinguishable
    from a working one. Printing a plain "ok 0 rows" for that is exactly the
    false pass this probe exists to avoid.

    Returns a note; empty means the evidence is good.
    """
    if raw_rows and not visible_rows:
        return ('%s raw transactions but the view exposes none, so the view '
                'check and the join check below prove nothing - they would '
                'return 0 even if the view were broken. Most likely the demo '
                'window is closed and every row is simulated; re-run with it '
                'open before trusting these two lines.'
                % raw_rows)
    if visible_rows and not joined_rows:
        return ('the view exposes %s rows but none of them join to '
                'atm_locations, which means atm_id does not line up. That is a '
                'real fault, not a vacuous check.' % visible_rows)
    return ''


def report(label, ok, detail=''):
    """ok is deliberately a string, not a bool, so a skipped check cannot be
    mistaken for a passing one."""
    mark = 'ok  ' if ok == 'yes' else 'FAIL'
    print('  %s %-34s %s' % (mark, label, detail), flush=True)
    if ok != 'yes':
        FAILURES.append(label)


def _wrap(text, width=74):
    words, line, out = text.split(), '', []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + ' ' + w).strip()
    if line:
        out.append(line)
    return out


def main():
    try:
        import psycopg2
    except ImportError:
        print('psycopg2 is missing. Run this inside a container that has it:')
        print('  docker exec -e DB_NAME=portal -i report-portal python3 -'
              ' < scripts/probe_portal_db.py')
        return 2

    # No default, for the same reason the other scripts refuse to guess: the
    # point of this probe is to prove a specific database is readable, and a
    # silent fallback to the pre-split one would have it measuring the wrong
    # thing. Inside the portal container DB_NAME is set; on the host, say so.
    dbname = os.environ.get('DB_NAME')
    if not dbname:
        print('DB_NAME is not set, so this cannot tell which database to probe.\n'
              'Run it as:\n'
              '    docker exec -e DB_NAME=portal -i report-portal python3 -'
              ' < scripts/probe_portal_db.py')
        return 2

    dsn = dict(
        host=os.environ.get('DB_HOST', 'postgres'),
        dbname=dbname,
        user=os.environ.get('DB_USER', 'zabbix'),
        password=os.environ.get('DB_PASS', ''),
    )
    conn = psycopg2.connect(**dsn)
    # readonly=True issues SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY,
    # which is what actually blocks writes. Verified against a real server,
    # because the obvious alternative - SET default_transaction_read_only = on -
    # does NOT work here: psql (and any driver) sends both statements in one
    # already-begun transaction, so the INSERT went straight through. A probe
    # that quietly writes would make the next --verify report a MISMATCH
    # against the manifest, with no obvious culprit.
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()

    # Prove which database this actually is. If the DB_NAME override did not
    # take, everything below would silently measure the OLD database and the
    # whole probe would be worthless.
    cur.execute('SELECT current_database()')
    actual = cur.fetchone()[0]
    print('Connected to database: %s (DB_NAME=%s, requested %s)'
          % (actual, os.environ.get('DB_NAME', '<unset>'), dsn['dbname']))
    if actual != dsn['dbname']:
        report('connected to the requested database', 'no',
               'landed in %s instead of %s' % (actual, dsn['dbname']))
        return 1
    report('connected to the requested database', 'yes', actual)

    # Every relation the list of record claims, read the way the portal reads it.
    counts = [
        ('atm_transactions_visible', 'the view most pages query'),
        ('atm_transactions', 'raw transactions'),
        ('atm_current_state', 'live device state'),
        ('atm_locations', 'the ATM registry'),
        ('app_users', 'portal accounts'),
        ('audit_log', 'audit trail'),
        ('atm_anomalies', 'detected anomalies'),
        ('atm_network_metrics', 'network metrics'),
        ('atm_network_correlation', 'correlations'),
        ('atm_network_events', 'network events'),
        ('atm_cassette_levels', 'cassette levels'),
        ('cash_replenishments', 'replenishments'),
        ('fault_type_map', 'vendor fault codes'),
        ('ticket_seen', 'ticket dedupe'),
        ('scheduled_reports', 'report schedules'),
        ('txn_source_policy', 'demo/real source policy'),
        ('atm_delete_archive', 'archive of deletions'),
        ('atm_state_history', 'state history'),
        ('fleet_report_cache', 'fleet cache'),
    ]
    for table, note in counts:
        try:
            cur.execute('SELECT count(*) FROM public.%s' % table)
            n = cur.fetchone()[0]
            report(table, 'yes', '%s rows  (%s)' % (n, note))
        except Exception as exc:                      # noqa: BLE001
            conn.rollback()
            report(table, 'no', str(exc).strip().splitlines()[0])
            cur = conn.cursor()

    # The join routes.py performs on essentially every page. A view can exist,
    # hold the right row count, and still not join.
    raw = visible = joined = 0
    try:
        cur.execute('SELECT count(*) FROM public.atm_transactions')
        raw = cur.fetchone()[0]
    except Exception:                                 # noqa: BLE001
        conn.rollback()
        cur = conn.cursor()
    try:
        cur.execute('SELECT count(*) FROM public.atm_transactions_visible')
        visible = cur.fetchone()[0]
    except Exception:                                 # noqa: BLE001
        conn.rollback()
        cur = conn.cursor()
    try:
        cur.execute("""
            SELECT count(*)
            FROM atm_transactions_visible t
            JOIN atm_locations l ON t.atm_id = l.atm_id
        """)
        joined = cur.fetchone()[0]
        report('view JOINs atm_locations', 'yes', '%s rows' % joined)
    except Exception as exc:                          # noqa: BLE001
        conn.rollback()
        report('view JOINs atm_locations', 'no', str(exc).strip().splitlines()[0])
        cur = conn.cursor()

    note = view_evidence(raw, visible, joined)
    if note:
        print()
        print('  NOTE   the two checks above are not evidence:')
        for line in _wrap(note):
            print('         %s' % line)
        NOTES.append(note)

    # The one that bites in production and nowhere else. Row counts match and
    # the portal still fails, because every insert collides with a row that is
    # already there. This is the check the earlier copy nearly failed.
    try:
        cur.execute('SELECT last_value, is_called FROM atm_transactions_id_seq')
        last, called = cur.fetchone()
        cur.execute('SELECT coalesce(max(id), 0) FROM atm_transactions')
        maxid = cur.fetchone()[0]
        if not called:
            report('transaction sequence usable', 'no',
                   'sequence has never been called; next id would be %s' % last)
        elif last < maxid:
            report('transaction sequence usable', 'no',
                   'sequence at %s but rows exist up to %s - next insert collides'
                   % (last, maxid))
        else:
            report('transaction sequence usable', 'yes',
                   'next id %s, rows end at %s' % (last + 1, maxid))
    except Exception as exc:                          # noqa: BLE001
        conn.rollback()
        report('transaction sequence usable', 'no',
               str(exc).strip().splitlines()[0])

    # The portal's own schema marker, read by txn_sources.py.
    try:
        cur.execute('SELECT version FROM portal_schema_version LIMIT 1')
        row = cur.fetchone()
        report('portal_schema_version readable', 'yes',
               'schema %s' % (row[0] if row else 'no row'))
    except Exception as exc:                          # noqa: BLE001
        conn.rollback()
        report('portal_schema_version readable', 'no',
               str(exc).strip().splitlines()[0])

    # Confirm the read-only transaction is real, rather than assumed.
    try:
        cur.execute("INSERT INTO portal_schema_version (version) VALUES (-1)")
        conn.rollback()
        report('transaction is read-only', 'no', 'a write succeeded')
    except Exception:                                 # noqa: BLE001
        conn.rollback()
        report('transaction is read-only', 'yes', 'writes rejected as intended')

    conn.rollback()
    conn.close()

    print()
    if FAILURES:
        print('%d check(s) failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        print('The copy is NOT ready for Stage 4.')
        return 1
    if NOTES:
        print('All checks passed, but %d of them proved nothing.' % len(NOTES))
        print('Re-run with the demo window open before treating this as proof.')
        return 0
    print('All checks passed against %s.' % actual)
    print('Stage 3 is a read-only proof. Nothing has been switched.')
    print('Stage 4 still needs: a quiet window, and network-correlator moved')
    print('to the same database (docker-compose.yml sets its DB_NAME to')
    print('"zabbix"; leave it there and metrics keep landing in the old')
    print('database while everything else reads the new one).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
