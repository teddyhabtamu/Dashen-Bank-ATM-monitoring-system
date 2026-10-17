#!/usr/bin/env python3
"""Alert on jobs that stopped running. Stdlib only - runs anywhere with Python 3.

Why this exists: the 06:00 Zabbix<->GLPI reconcile cron failed every day for
about 19 days because it invoked `venv/bin/python`, which does not exist on
that host. Nothing failed loudly. It wrote to logs/reconcile.log, and nobody
reads that file, so the system looked perfectly healthy while daily
reconciliation did nothing.

That is the failure class this closes: a job that stops, silently. It does not
restart anything and it does not retry - it reports, and something alerts on the
report. Auto-restarting a job that has been failing for three weeks is how you
turn a small mistake into a large surprise.

Usage:

    python3 scripts/check_jobs.py                 # human summary, exit 1 if stale
    python3 scripts/check_jobs.py --json          # machine readable
    python3 scripts/check_jobs.py --quiet         # exit code only, for cron

Wire it to whatever alerts you use (a cron entry that mails, an Alertmanager
webhook, a Slack/Teams hook). The exit code is the contract:

    0 = every tracked job ran inside its window
    1 = at least one job is overdue

Note on what counts as "ran": a job that exits non-zero still updates its log,
so a FAILED job is reported separately from a job that never started. Those are
different faults and get different remedies.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

# Each job: the log it writes, and the longest we tolerate silence.
#
# Intervals are the SCHEDULE, not the observed runtime. A job scheduled hourly
# that last ran four hours ago is broken; a job scheduled daily that last ran
# 25 hours ago is broken. Multipliers are deliberately generous - this should
# fire on real outages, not on a slow machine.
JOBS = (
    # name,             log path,                     schedule seconds,   grace x
    ('ej_pull',         'logs/ej-pull.log',            60,                 6),
    ('reconcile_glpi',  'logs/reconcile.log',          86400,              2),
    ('backup_db',       'atm-backup.log',              86400,              2),
    ('backup_configs',  'atm-backup.log',              86400,              2),
)

# Resolved from this script's own location, not the cwd: cron runs from the
# repo directory today, but a relative path means the checker silently watches a
# different tree the moment someone runs it from anywhere else. Overridable so
# the same logic can be pointed at a restored copy, or a test fixture.
ROOT = os.environ.get(
    'CHECK_JOBS_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def age_seconds(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def evaluate(now=None):
    now = now or time.time()
    results = []
    for name, rel, interval, grace in JOBS:
        path = os.path.join(ROOT, rel)
        age = age_seconds(path)
        limit = interval * grace
        if age is None:
            results.append({
                'job': name, 'log': rel, 'state': 'NEVER_RAN',
                'detail': 'no log file at %s - the job has never written here' % rel,
                'overdue_by_s': None, 'limit_s': limit,
            })
            continue
        if age > limit:
            results.append({
                'job': name, 'log': rel, 'state': 'OVERDUE',
                'detail': 'last wrote %.1f min ago; expected at least every %.0f min'
                          % (age / 60.0, interval / 60.0),
                'overdue_by_s': int(age - limit), 'limit_s': limit,
            })
            continue
        # Recent activity. Report a FAILED run separately - the log exists, so
        # the job started and something failed inside it.
        state, detail = 'ok', 'last wrote %.1f min ago' % (age / 60.0)
        try:
            with open(path, 'rb') as fh:
                try:
                    fh.seek(-4096, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                tail = fh.read().decode('utf-8', 'replace')
            low = tail.lower()
            for marker, label in ((b'no such file or directory', 'command not found'),
                                  (b'not found', 'command not found'),
                                  (b'traceback', 'python traceback'),
                                  (b'command not found', 'command not found'),
                                  (b'permission denied', 'permission denied'),
                                  (b'connection refused', 'cannot connect'),
                                  (b'relation .* does not exist', 'missing table')):
                if re_search(low, marker):
                    state, detail = 'FAILED', 'recent log contains: %s' % label
                    break
        except OSError:
            pass
        results.append({
            'job': name, 'log': rel, 'state': state, 'detail': detail,
            'overdue_by_s': 0, 'limit_s': limit,
        })
    return results


def re_search(haystack, needle_bytes):
    import re
    return re.search(needle_bytes.decode(), haystack) is not None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json', action='store_true', help='machine readable output')
    ap.add_argument('--quiet', action='store_true', help='print nothing; exit code only')
    args = ap.parse_args()

    results = evaluate()
    bad = [r for r in results if r['state'] != 'ok']

    if args.json:
        print(json.dumps({'checked': len(results), 'problems': bad,
                          'results': results}, indent=2))
    elif not args.quiet:
        if not bad:
            print('all %d tracked jobs ran within their window' % len(results))
            for r in results:
                print('  ok      %-16s %s' % (r['job'], r['detail']))
        else:
            print('%d of %d tracked jobs need attention\n' % (len(bad), len(results)))
            for r in results:
                mark = '  ' if r['state'] == 'ok' else '!!'
                print('%s %-8s %-16s %s' % (mark, r['state'], r['job'], r['detail']))
            print('\nLikely causes, cheapest first:')
            print('  * the command in the crontab does not exist (check with `which`)')
            print('  * the script moved, or the path in crontab is relative to the wrong dir')
            print('  * no venv: crontab uses venv/bin/python but the host has none -')
            print('    use `python3` on the host, or pipe the script into the container')
            print('  * permissions on logs/')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())