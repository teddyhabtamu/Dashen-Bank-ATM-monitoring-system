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
# Paths are relative to ROOT unless they start with $HOME, ~ or /.
#
# backup_db and backup_configs write outside the repo, under $HOME.
# The first version of this file assumed a repo-relative path and reported both
# as NEVER_RAN on a host where the backups were running perfectly. A monitor
# that cries wolf is worse than none, so paths are overridable per job:
#
#     CHECK_JOBS_LOG_backup_db=/root/atm-backup.log python3 scripts/check_jobs.py
#
# Each job gets its OWN log. They used to share $HOME/atm-backup.log, which
# meant a failed database backup at 02:00 could be buried by a successful config
# backup at 02:30 - the log ends looking fine, and the failure is invisible
# until someone needs the data. backup_db.sh is the only thing covering the
# portal database since the split, so this is the one job whose failure must
# never be masked. Separate paths turn that from a warning into a visible
# "never ran" if the crontab has not been updated to match.
#
JOBS = (
    # name,             log path,                     schedule seconds,   grace x
    ('ej_pull',         'logs/ej-pull.log',            60,                 6),
    ('grg_ej_pull',     'logs/grg-ej-pull.log',        300,                 6),
    ('reconcile_glpi',  'logs/reconcile.log',          86400,              2),
    ('backup_db',       '$HOME/atm-backup_db.log',     86400,              2),
    ('backup_configs',  '$HOME/atm-backup_configs.log', 86400,             2),
)

# Resolved from this script's own location, not the cwd: cron runs from the
# repo directory today, but a relative path means the checker silently watches a
# different tree the moment someone runs it from anywhere else. Overridable so
# the same logic can be pointed at a restored copy, or a test fixture.
ROOT = os.environ.get(
    'CHECK_JOBS_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def age_seconds(path):
    """How long since this file was last written. None if it does not exist.

    None is deliberately distinct from 0: "no log at all" is a different fault
    from "the log went quiet", and they have different causes.
    """
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def resolve(rel, job):
    """Absolute path for a job's log.

    $HOME and ~ are expanded because cron runs as its own user, which is very
    often not the user running this check. An override wins over everything.
    """
    override = os.environ.get('CHECK_JOBS_LOG_%s' % job)
    if override:
        return override
    if rel.startswith('$HOME/'):
        # os.path.expanduser handles ~ but NOT $HOME - verified, and the first
        # version of this silently looked for a file literally named "$HOME".
        return os.path.join(os.environ.get('HOME', '/root'), rel[6:])
    if rel.startswith('~/'):
        return os.path.expanduser(rel)
    if rel.startswith('/'):
        return rel
    return os.path.join(ROOT, rel)


def shared_logs():
    """Jobs whose logs are the same file.

    Two jobs sharing one log CANNOT be verified independently: the mtime
    updates when either runs, so a job that fails every night looks healthy for
    as long as its neighbour succeeds. That is a false NEGATIVE - it hides a
    fault - which is more dangerous than the false positive above.
    """
    # Keyed on the RESOLVED path, not the configured one. The per-job override
    # exists precisely to correct a path, and an operator who corrects both
    # jobs onto the same file - an easy mistake, since it looks tidier - would
    # otherwise get no warning at all. Reading the configured value instead
    # meant the detector could only ever see the defaults, so it was blind to
    # exactly the case the override makes possible.
    seen = {}
    for name, rel, _, _ in JOBS:
        seen.setdefault(os.path.basename(resolve(rel, name)), []).append(name)
    return {k: v for k, v in seen.items() if len(v) > 1}


def evaluate(now=None):
    now = now or time.time()
    results = []
    shared = shared_logs()
    for name, rel, interval, grace in JOBS:
        path = resolve(rel, name)
        age = age_seconds(path)
        limit = interval * grace
        if age is None:
            results.append({
                'job': name, 'log': path, 'state': 'NEVER_RAN',
                'detail': 'no log file at %s (cron writes there only if the job '
                          'succeeds; check the crontab path and the user it runs as)'
                          % path,
                'overdue_by_s': None, 'limit_s': limit,
                'shared_with': [n for n in shared.get(os.path.basename(path), [])
                                if n != name],
            })
            continue
        if age > limit:
            results.append({
                'job': name, 'log': path, 'state': 'OVERDUE',
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
            reason = failure_reason(tail)
            if reason:
                state, detail = 'FAILED', 'recent log contains: %s' % reason
        except OSError:
            pass
        others = [n for n in shared.get(os.path.basename(path), []) if n != name]
        if others:
            detail = ("%s; SHARED LOG with %s - a failure in one is invisible "
                      "while the other succeeds. Give each job its own log."
                      % (detail, ', '.join(others)))
        results.append({
            'job': name, 'log': path, 'state': state, 'detail': detail,
            'overdue_by_s': 0, 'limit_s': limit,
            'shared_with': others,
        })
    return results


def re_search(haystack, needle_bytes):
    import re
    return re.search(needle_bytes.decode(), haystack) is not None


def failure_reason(text):
    """Classify a recent log tail, or return None when it looks healthy.

    "No such file or directory" from a Zabbix item is a missing evidence file,
    not a missing shell command. The old mapping sent operators looking at
    PATH when the puller had actually run and the ATM had not produced the
    journal.
    """
    low = (text or '').lower()
    if 'command not found' in low:
        return 'command not found'
    if 'no such file or directory' in low:
        # A Zabbix item that cannot open a journal is missing evidence; a
        # shell's "<program>: No such file or directory" is a missing command.
        if 'zbx_notsupported' in low:
            return 'missing file/path'
        if ': no such file or directory' in low:
            return 'command not found'
        return 'missing file/path'
    for marker, label in ((b'not found', 'missing file or command'),
                          (b'traceback', 'python traceback'),
                          (b'permission denied', 'permission denied'),
                          (b'connection refused', 'cannot connect'),
                          (b'relation .* does not exist', 'missing table')):
        if re_search(low, marker):
            return label
    return None


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
            print('  * the log path here does not match the crontab - cron runs as')
            print('    its own user, so $HOME may not be yours; override with')
            print('    CHECK_JOBS_LOG_<job>=/actual/path')
            print('  * the command in the crontab does not exist (check with `which`)')
            print('  * the script moved, or the path in crontab is relative to the wrong dir')
            print('  * no venv: crontab uses venv/bin/python but the host has none -')
            print('    use `python3` on the host, or pipe the script into the container')
            print('  * permissions on logs/')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())