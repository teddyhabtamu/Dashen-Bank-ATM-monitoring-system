#!/usr/bin/env python3
"""Show, hide and audit which transaction sources the portal may display.

The portal reads atm_transactions_visible, a view that only exposes sources
enabled in txn_source_policy. This script is the only supported way to change
that policy, so that "why am I seeing invented transactions?" always has an
audit trail behind the answer.

Usage (from the repo root, on the monitoring host):

    python3 scripts/set_txn_sources.py                      # what is visible now
    python3 scripts/set_txn_sources.py --hide-demo          # back to real only
    python3 scripts/set_txn_sources.py --show-demo SIMULATOR --i-know-this-is-demo

Showing simulated data requires --i-know-this-is-demo on purpose. The portal
renders a red banner while it is on, but the operator should have to mean it:
a cash replenishment decision taken on invented withdrawals is the exact failure
this policy exists to prevent.

Run it inside the portal container, which already has the DB credentials:

    docker exec report-portal python3 scripts/set_txn_sources.py --hide-demo
"""
import argparse
import datetime
import os
import sys


def _find_portal_dir():
    """Locate report-portal/ so this script runs from anywhere.

    The host has no venv and the container has no scripts/, so the only way to
    run a DB-dependent script is `docker exec -i report-portal python3 - < this
    file`. Under `python3 -` there is no __file__ at all, and the portal lives at
    /app inside the container but at ../report-portal on the host.
    """
    here = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else ''
    for cand in (os.environ.get('PORTAL_DIR'), '/app',
                 os.path.join(here, '..', 'report-portal'),
                 os.path.join(os.getcwd(), 'report-portal'), here):
        if cand and os.path.isfile(os.path.join(cand, 'db.py')):
            return cand
    sys.exit('Could not find report-portal/. Set PORTAL_DIR to its path.')


sys.path.insert(0, _find_portal_dir())

try:
    from db import get_db  # noqa: E402
    from txn_sources import TRUSTED_SOURCES  # noqa: E402
except ImportError as e:
    # Almost always "psycopg2 is missing", which means this ran somewhere it
    # cannot reach the database: the UAT host has no venv, and the container
    # that does have psycopg2 has no scripts/. Say so instead of dumping a
    # traceback about a module the operator does not know they need.
    sys.exit("Cannot import the portal database driver (%s).\n"
             "Run this inside the portal container:\n"
             "  docker exec -i report-portal python3 - < scripts/%s"
             % (e, os.path.basename(__file__)))

DEMO = ('SIMULATOR', 'ISO8583_SIM', 'ISO8583_REAL')


def show(conn):
    """Report EFFECTIVE visibility, not the raw enabled flag.

    This printed `enabled` under a heading called VISIBLE, and warned that
    simulated data was on screen when the demo window had already closed - while
    the SHOWN column on the same line said 0. On UAT the expiring demo window had
    correctly hidden everything and this tool claimed the opposite.

    A monitor that contradicts the thing it monitors is worse than no monitor,
    so the demo predicate is evaluated here exactly as the view evaluates it.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.source, p.label, p.trusted, p.enabled, p.demo_expires_at,
                   (SELECT count(*) FROM atm_transactions t WHERE t.source = p.source),
                   (SELECT count(*) FROM atm_transactions_visible v WHERE v.source = p.source)
              FROM txn_source_policy p
             ORDER BY p.enabled DESC, p.trusted DESC, p.source""")
        raw = cur.fetchall()

    if not raw:
        print('txn_source_policy is empty - run scripts/migrate_txn_sources.sql first.')
        return 1

    rows = []
    for src, label, trusted, enabled, expires, present, shown in raw:
        window_open = expires is not None and expires > now
        live = enabled and (trusted or window_open)
        rows.append((src, label, trusted, enabled, live, window_open, present, shown))

    print('%-14s %-7s %-9s %-10s %10s %9s  %s'
          % ('SOURCE', 'TRUSTED', 'ON SCREEN', 'ENABLED', 'ROWS', 'SHOWN', 'LABEL'))
    for src, label, trusted, enabled, live, window_open, present, shown in rows:
        state = 'yes' if live else 'no'
        if enabled and not live:
            state = 'no (window closed)' if window_open is False and expires \
                else 'no'
        print('%-14s %-7s %-9s %-10s %10s %9s  %s' % (
            src, 'yes' if trusted else 'NO', state,
            'yes' if enabled else 'no',
            f'{present:,}', f'{shown:,}', label))

    hidden = sum(r[6] for r in rows) - sum(r[7] for r in rows)
    print('\n%d row(s) present but hidden.' % hidden)
    demo_on = [r[0] for r in rows if r[4] and not r[2]]
    if demo_on:
        print('WARNING: simulated data IS on screen (%s) - red banner in the portal.'
              % ', '.join(demo_on))
    else:
        print('No simulated data on screen. The portal shows real sources only.')
    return 0


# Far future rather than NULL. NULL means "no demo window", i.e. invisible, so
# a demo can only be made permanent by being explicit about it.
NEVER_EXPIRES = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)


def _parse_until(value):
    """Accept a duration (4h, 90m) or a wall clock time today (16:30)."""
    if value == 'never':
        return NEVER_EXPIRES
    now = datetime.datetime.now(datetime.timezone.utc)
    if value.endswith(('h', 'm')) and value[:-1].isdigit():
        delta = datetime.timedelta(hours=int(value[:-1])) if value.endswith('h') \
            else datetime.timedelta(minutes=int(value[:-1]))
        return now + delta
    try:
        hh, _, mm = value.partition(':')
        when = now.astimezone(datetime.timezone(datetime.timedelta(hours=3)))
        target = when.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= when:
            target += datetime.timedelta(days=1)
        return target.astimezone(datetime.timezone.utc)
    except Exception:
        raise SystemExit('--until must look like 4h, 90m, 16:30 or never')


def set_enabled(conn, sources, enabled, actor, ack, until=None):
    with conn.cursor() as cur:
        cur.execute("SELECT source, label, trusted FROM txn_source_policy WHERE source = ANY(%s)",
                    (list(sources),))
        known = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    unknown = [s for s in sources if s not in known]
    if unknown:
        print('Unknown source(s): %s' % ', '.join(unknown))
        return 1
    untrusted = [s for s in sources if not known[s][1]]
    if enabled and untrusted and not ack:
        print('Refusing to show simulated data without --i-know-this-is-demo.')
        print('Sources that are not real bank data: %s' % ', '.join(untrusted))
        return 1
    expires = until if enabled else None
    with conn.cursor() as cur:
        cur.execute("UPDATE txn_source_policy SET enabled = %s, demo_expires_at = %s "
                    "WHERE source = ANY(%s)",
                    (enabled, expires, list(sources)))
    conn.commit()
    detail = '%s %s by %s' % ('enabled' if enabled else 'disabled',
                              ','.join(sorted(sources)), actor)
    if enabled and expires:
        detail += ' (demo window ends %s)' % expires.astimezone(
            datetime.timezone(datetime.timedelta(hours=3))).strftime('%Y-%m-%d %H:%M EAT')
    try:
        from audit import log_action
        log_action('TXN_SOURCES', detail)
    except Exception as e:
        print('WARNING: change applied but not audited (%s)' % e)
    print(detail)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hide-demo', action='store_true',
                    help='show only real sources (%s)' % ', '.join(TRUSTED_SOURCES))
    ap.add_argument('--show-demo', nargs='+', metavar='SOURCE',
                    help='enable these sources (normally a simulated one)')
    ap.add_argument('--i-know-this-is-demo', action='store_true',
                    help='required to enable a source that is not real bank data')
    ap.add_argument('--until', metavar='WHEN',
                    help='how long the demo window stays open: 4h, 90m, 16:30, or '
                         'never. Required with --show-demo for simulated sources, so '
                         'a demo cannot outlive itself.')
    ap.add_argument('--actor', default=os.environ.get('USER', 'cli'),
                    help='who is making the change (recorded in the audit log)')
    args = ap.parse_args()

    if args.hide_demo and args.show_demo:
        ap.error('choose one of --hide-demo or --show-demo')

    with get_db() as conn:
        if args.hide_demo:
            return set_enabled(conn, DEMO, False, args.actor, True)
        if args.show_demo:
            untrusted_demo = any(s in DEMO for s in args.show_demo)
            if untrusted_demo and not args.until:
                ap.error('--show-demo of simulated data needs --until (e.g. 3h or '
                         '16:30), so demo mode cannot outlive the demo. Pass '
                         '--until never if you truly want it permanent.')
            return set_enabled(conn, args.show_demo, True, args.actor,
                               args.i_know_this_is_demo,
                               _parse_until(args.until) if args.until else None)
        return show(conn)


if __name__ == '__main__':
    sys.exit(main())
