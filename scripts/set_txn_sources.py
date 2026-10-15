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
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'report-portal'))

from db import get_db  # noqa: E402
from txn_sources import TRUSTED_SOURCES  # noqa: E402

DEMO = ('SIMULATOR', 'ISO8583_SIM', 'ISO8583_REAL')


def show(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.source, p.label, p.trusted, p.enabled,
                   (SELECT count(*) FROM atm_transactions t WHERE t.source = p.source),
                   (SELECT count(*) FROM atm_transactions_visible v WHERE v.source = p.source)
              FROM txn_source_policy p
             ORDER BY p.enabled DESC, p.trusted DESC, p.source""")
        rows = cur.fetchall()
    if not rows:
        print('txn_source_policy is empty - run scripts/migrate_txn_sources.sql first.')
        return 1
    print('%-14s %-6s %-8s %10s %10s  %s' % ('SOURCE', 'TRUSTED', 'VISIBLE', 'ROWS', 'SHOWN', 'LABEL'))
    for src, label, trusted, enabled, present, shown in rows:
        print('%-14s %-6s %-8s %10s %10s  %s' % (
            src, 'yes' if trusted else 'NO', 'yes' if enabled else 'no',
            f'{present:,}', f'{shown:,}', label))
    hidden = sum(r[4] for r in rows) - sum(r[5] for r in rows)
    print('\n%d row(s) present but hidden.' % hidden)
    if any(r[3] and not r[2] for r in rows):
        print('WARNING: simulated data is currently visible (red banner in the portal).')
    return 0


def set_enabled(conn, sources, enabled, actor, ack):
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
    with conn.cursor() as cur:
        cur.execute("UPDATE txn_source_policy SET enabled = %s WHERE source = ANY(%s)",
                    (enabled, list(sources)))
    conn.commit()
    detail = '%s %s by %s' % ('enabled' if enabled else 'disabled',
                              ','.join(sorted(sources)), actor)
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
    ap.add_argument('--actor', default=os.environ.get('USER', 'cli'),
                    help='who is making the change (recorded in the audit log)')
    args = ap.parse_args()

    if args.hide_demo and args.show_demo:
        ap.error('choose one of --hide-demo or --show-demo')

    with get_db() as conn:
        if args.hide_demo:
            return set_enabled(conn, DEMO, False, args.actor, True)
        if args.show_demo:
            return set_enabled(conn, args.show_demo, True, args.actor, args.i_know_this_is_demo)
        return show(conn)


if __name__ == '__main__':
    sys.exit(main())
