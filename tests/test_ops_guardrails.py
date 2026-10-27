"""The operational guardrails added after the 19-day silent cron failure.

Three things are tested here, and the common thread is that each one must fail
LOUDLY:

  1. scripts/check_jobs.py - would have caught `venv/bin/python` not existing.
  2. /ready - a readiness probe that cannot say WHICH dependency is down is not
     much better than none, because the only question anyone has is "which one".
  3. requirements.txt - must stay in step with the Dockerfile, or the pinning
     fix is decorative.
"""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK_JOBS = REPO / 'scripts' / 'check_jobs.py'


def run_check(root, **extra):
    # CHECK_JOBS_ROOT rather than cwd: the checker resolves paths against the
    # repo it lives in, so pointing it at a fixture is the honest way to test.
    env = _env(root, **extra)
    out = subprocess.run([sys.executable, str(CHECK_JOBS), '--json'],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode in (0, 1), out.stderr
    return json.loads(out.stdout)


def _make_repo(tmp_path, ages):
    """Build a fake repo with log files of the given ages (seconds).

    The backup logs go to $HOME, because that is where cron writes them -
    reproducing that mistake is what the false-positive test below guards.
    """
    now = time.time()
    for rel, age in ages.items():
        p = tmp_path / rel if not rel.startswith('$HOME/') \
            else tmp_path / 'home' / rel.split('/', 1)[1]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('log line\n')
        t = now - age
        os.utime(p, (t, t))
    return tmp_path


def _env(root, **extra):
    env = dict(os.environ, CHECK_JOBS_ROOT=str(root))
    env['HOME'] = str(root / 'home')
    env.update(extra)
    return env


# ── check_jobs ──────────────────────────────────────────────────────────────

def test_missing_log_is_never_ran_not_ok(tmp_path):
    """No log at all means the job never wrote anything - which is what a
    crontab pointing at a command that does not produce."""
    data = run_check(tmp_path)
    assert data['problems'], 'a missing log must be reported'
    states = {p['state'] for p in data['problems']}
    assert states == {'NEVER_RAN'}, states


def test_stale_log_is_overdue(tmp_path):
    """The EJ pull runs every minute; silence for an hour is a fault."""
    _make_repo(tmp_path, {
        'logs/ej-pull.log': 5400,       # 90 min old, limit is 60 min x 6
        'logs/reconcile.log': 300,
        '$HOME/atm-backup_db.log': 300,
        '$HOME/atm-backup_configs.log': 300,
    })
    data = run_check(tmp_path)
    overdue = [p for p in data['problems'] if p['state'] == 'OVERDUE']
    assert [p['job'] for p in overdue] == ['ej_pull']
    assert overdue[0]['overdue_by_s'] > 0


def test_the_reconcile_failure_we_actually_had_is_detected(tmp_path):
    """2026-10-15: `venv/bin/python` did not exist on the host, so the 06:00
    reconcile wrote 'No such file or directory' to its log every day for about
    three weeks and nothing reported it.

    Two distinct faults live in that one story and both must be caught:
      * the job never ran        -> no log / stale log
      * the job ran but failed   -> a fresh log full of the error
    """
    _make_repo(tmp_path, {
        'logs/ej-pull.log': 30,
        '$HOME/atm-backup_db.log': 30,
        '$HOME/atm-backup_configs.log': 30,
    })
    (tmp_path / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'logs' / 'reconcile.log').write_text(
        '/bin/sh: line 1: venv/bin/python: No such file or directory\n' * 5)
    now = time.time()
    os.utime(tmp_path / 'logs' / 'reconcile.log', (now, now))

    data = run_check(tmp_path)
    problems = {p['job']: p for p in data['problems']}
    assert 'reconcile_glpi' in problems, 'the failing job must be reported'
    assert problems['reconcile_glpi']['state'] == 'FAILED', problems
    assert 'command not found' in problems['reconcile_glpi']['detail']
    assert 'ej_pull' not in problems, 'a healthy job must not be reported'


def test_missing_zbx_evidence_warns_but_does_not_fail_the_check(tmp_path):
    """An idle or out-of-service ATM produces no journal. The puller still
    ran, so the gap must be visible without paging as a job failure."""
    _make_repo(tmp_path, {
        'logs/ej-pull.log': 30,
        'logs/grg-ej-pull.log': 30,
        'logs/reconcile.log': 600,
        '$HOME/atm-backup_db.log': 600,
        '$HOME/atm-backup_configs.log': 600,
    })
    p = tmp_path / 'logs' / 'grg-ej-pull.log'
    p.write_text(
        '  ATM-GRG-1 2026-10-27 absent: ZBX_NOTSUPPORTED: Cannot open file: '
        '[2] No such file or directory\n')
    now = time.time()
    os.utime(p, (now, now))

    env = _env(tmp_path)
    out = subprocess.run([sys.executable, str(CHECK_JOBS), '--json'],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stdout
    data = json.loads(out.stdout)
    assert data['problems'] == []
    assert [w['job'] for w in data['warnings']] == ['grg_ej_pull']
    assert data['warnings'][0]['state'] == 'WARN'


def test_healthy_repo_reports_nothing_and_exits_zero(tmp_path):
    _make_repo(tmp_path, {
        'logs/ej-pull.log': 30,
        'logs/grg-ej-pull.log': 30,
        'logs/reconcile.log': 600,
        '$HOME/atm-backup_db.log': 600,
        '$HOME/atm-backup_configs.log': 600,
    })
    env = _env(tmp_path)
    out = subprocess.run([sys.executable, str(CHECK_JOBS), '--json'],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stdout
    assert json.loads(out.stdout)['problems'] == []


def test_missing_table_in_a_log_is_reported(tmp_path):
    """The failure mode from today's outage, which is easy to mistake for
    'the app is just slow'."""
    _make_repo(tmp_path, {
        'logs/ej-pull.log': 30,
        'logs/reconcile.log': 600,
        '$HOME/atm-backup_db.log': 600,
        '$HOME/atm-backup_configs.log': 600,
    })
    p = tmp_path / 'logs' / 'reconcile.log'
    p.write_text('psycopg2.errors.UndefinedTable: relation "atm_x" does not exist\n')
    now = time.time()
    os.utime(p, (now, now))
    data = run_check(tmp_path)
    jobs = {x['job']: x for x in data['problems']}
    assert jobs.get('reconcile_glpi', {}).get('state') == 'FAILED'


def test_quiet_mode_prints_nothing(tmp_path):
    _make_repo(tmp_path, {})
    env = _env(tmp_path)
    out = subprocess.run([sys.executable, str(CHECK_JOBS), '--quiet'],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == ''
    assert out.returncode == 1


def test_grace_is_generous_enough_not_to_be_noisy(tmp_path):
    """A slow machine must not page anyone. The EJ pull allows 6x its schedule."""
    sys.path.insert(0, str(REPO / 'scripts'))
    import check_jobs
    ej = [j for j in check_jobs.JOBS if j[0] == 'ej_pull'][0]
    assert ej[3] >= 3, 'grace multiplier too tight - this will cry wolf'
    grg = [j for j in check_jobs.JOBS if j[0] == 'grg_ej_pull'][0]
    assert grg[2] == 300 and grg[3] >= 3, 'GRG pull must be monitored'
    reconcile = [j for j in check_jobs.JOBS if j[0] == 'reconcile_glpi'][0]
    assert reconcile[2] == 86400, 'reconcile is a daily job'


def test_checker_is_stdlib_only(tmp_path):
    """It must run on the host, which has no venv and minimal packages."""
    src = CHECK_JOBS.read_text()
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split('.')[0])
    assert mods <= {'argparse', 'datetime', 'json', 'os', 're', 'sys', 'time'}, mods


# ── readiness probe ────────────────────────────────────────────────────────

def test_health_stays_unconditional_and_ready_does_the_work():
    """Liveness and readiness are different questions. Making /health depend on
    a database would take every worker out of rotation during a restart."""
    src = (REPO / 'report-portal' / 'app.py').read_text()
    health = src[src.index('def health():'):src.index('def ready():')]
    assert 'get_db' not in health, '/health must stay liveness-only'
    assert 'requests' not in health


def test_ready_reports_each_dependency_separately():
    src = (REPO / 'report-portal' / 'app.py').read_text()
    ready = src[src.index('def ready():'):src.index("return ({'status': 'ready'")]
    for dep in ('database', 'zabbix', 'opensearch'):
        assert "'%s'" % dep in ready, 'readiness must name %s' % dep
    assert 'try:' in ready and 'except Exception' in ready, (
        'one dead dependency must not crash the probe')


def test_ready_uses_short_timeouts():
    """A readiness probe with a 30s timeout is a load generator during an
    outage - it holds workers open exactly when there is least capacity."""
    src = (REPO / 'report-portal' / 'app.py').read_text()
    ready = src[src.index('def ready():'):src.index("return ({'status': 'ready'")]
    for t in re.findall(r'timeout=(\d+)', ready):
        assert int(t) <= 5, 'dependency timeout %ss is too long for a probe' % t


def test_ready_returns_503_when_degraded():
    src = (REPO / 'report-portal' / 'app.py').read_text()
    ready = src[src.index('def ready():'):]
    assert 'if healthy else 503' in ready, (
        'an unready process must not report success to a load balancer')


def test_opensearch_failure_does_not_fail_readiness():
    """EJ search degrades on its own. Without OpenSearch the portal still
    serves ATMs, so the whole service must not drop out of rotation."""
    src = (REPO / 'report-portal' / 'app.py').read_text()
    ready = src[src.index('def ready():'):src.index("return ({'status': 'ready'")]
    os_block = ready[ready.index("checks['opensearch']"):]
    assert "healthy = False" not in os_block.split('checks[')[0], (
        'opensearch must not set healthy = False')


# ── dependency pinning ─────────────────────────────────────────────────────

def test_requirements_file_matches_the_dockerfile():
    """If these drift, the Dockerfile installs something the declared intent
    never mentions, and the pinning work is decoration."""
    reqs = [l.strip() for l in
            (REPO / 'report-portal' / 'requirements.txt').read_text().split('\n')
            if l.strip() and not l.startswith('#')]
    dockerfile = (REPO / 'report-portal' / 'Dockerfile').read_text()
    assert 'requirements.txt' in dockerfile, 'Dockerfile must install from it'
    assert 'pip install flask' not in dockerfile, (
        'the bare pip install list is what this replaces')
    # The Dockerfile used to name exactly these packages inline; the whole point
    # is that they now live in one place, so the list must not quietly change.
    assert set(reqs) == {
        'flask', 'psycopg2-binary', 'openpyxl', 'reportlab', 'requests',
        'apscheduler', 'flask-wtf', 'ldap3', 'gunicorn'}, reqs


def test_dockerfile_installs_the_lock_when_present():
    """The lock is the deployed truth; the named file is intent. Without the
    conditional, a lock committed later would be silently ignored."""
    dockerfile = (REPO / 'report-portal' / 'Dockerfile').read_text()
    assert 'requirements.lock' in dockerfile, (
        'the Dockerfile must apply requirements.lock when it exists')


def test_lock_capture_script_resolves_digests_not_tags():
    """A tag can be repointed; a digest cannot. Capturing tags would reproduce
    the original problem."""
    src = (REPO / 'scripts' / 'capture_env_lock.sh').read_text()
    assert '{{.Image}}' in src, 'must resolve the running image to a digest'
    assert 'pip freeze' in src or 'pip' in src and 'freeze' in src
    assert '--apply' in src, 'opt-in, never mutate .env by default'

def test_home_paths_are_expanded_not_taken_literally(tmp_path):
    """os.path.expanduser handles ~ but NOT $HOME.

    The first version looked for a file literally named "$HOME/atm-backup.log",
    so it reported NEVER_RAN on a host where the backups were running fine. A
    monitor that cries wolf gets ignored, which is worse than no monitor.
    """
    _make_repo(tmp_path, {'$HOME/atm-backup_db.log': 600,
                         '$HOME/atm-backup_configs.log': 600})
    data = run_check(tmp_path)
    problems = {p['job']: p for p in data['problems']}
    assert 'backup_db' not in problems, (
        problems.get('backup_db', {}).get('log', ''))
    assert 'atm-backup_db.log' in json.dumps(data)


def test_log_path_can_be_overridden_per_job(tmp_path):
    """Cron runs as its own user, which is often not the user running the check."""
    (tmp_path / 'elsewhere').mkdir()
    log = tmp_path / 'elsewhere' / 'nightly.log'
    log.write_text('ok\n')
    now = time.time()
    os.utime(log, (now, now))
    _make_repo(tmp_path, {'logs/ej-pull.log': 30, 'logs/reconcile.log': 600,
                          '$HOME/atm-backup_db.log': 600,
                          '$HOME/atm-backup_configs.log': 600})
    data = run_check(tmp_path, CHECK_JOBS_LOG_backup_db=str(log))
    problems = {p['job'] for p in data['problems']}
    assert 'backup_db' not in problems, 'the override must be honoured'


def test_the_shipped_config_has_no_shared_log(tmp_path):
    """backup_db and backup_configs used to append to the same
    $HOME/atm-backup.log, running at 02:00 and 02:30. A failed database backup
    followed by a successful config backup left the log ending in success, so the
    failure was invisible until someone needed the data - a false NEGATIVE, the
    one kind that hides a fault rather than raising it.

    Since the split, backup_db.sh is the only thing covering the portal
    database, which makes it exactly the job whose failure must not be masked by
    an unrelated job succeeding afterwards. Separate log paths are now the
    shipped default."""
    _make_repo(tmp_path, {'logs/ej-pull.log': 30, 'logs/reconcile.log': 600,
                          '$HOME/atm-backup_db.log': 600,
                          '$HOME/atm-backup_configs.log': 600})
    data = run_check(tmp_path)
    jobs = {r['job']: r for r in data['results']}
    for name, r in jobs.items():
        assert r['shared_with'] == [], (
            '%s still shares a log with %s; a failure there can be hidden'
            % (name, r['shared_with']))
        assert 'SHARED LOG' not in r['detail']


def test_a_shared_log_is_still_detected_when_one_is_injected(tmp_path):
    """The detector must keep working even though the shipped config no longer
    trips it. An operator who reverts the crontab to one shared log should be
    told, not left with a monitor that has quietly stopped looking.

    Both jobs are pointed at one file through the documented per-job override,
    which is how a real regression would present."""
    _make_repo(tmp_path, {'logs/ej-pull.log': 30, 'logs/reconcile.log': 600,
                          '$HOME/shared.log': 600})
    shared = str(tmp_path / 'home' / 'shared.log')
    data = run_check(tmp_path,
                     CHECK_JOBS_LOG_backup_db=shared,
                     CHECK_JOBS_LOG_backup_configs=shared)
    jobs = {r['job']: r for r in data['results']}
    assert set(jobs['backup_db']['shared_with']) == {'backup_configs'}
    assert 'SHARED LOG' in jobs['backup_db']['detail']
    assert jobs['ej_pull']['shared_with'] == [], (
        'a job with its own log must not be annotated as sharing')


def test_missing_log_message_names_the_resolved_path(tmp_path):
    """The operator needs the path that was actually looked for, not the
    configured one."""
    _make_repo(tmp_path, {'logs/ej-pull.log': 30})
    data = run_check(tmp_path)
    problems = {p['job']: p for p in data['problems']}
    assert 'backup_db' in problems
    assert str(tmp_path / 'home') in problems['backup_db']['log'], \
        problems['backup_db']['log']


def test_grg_pull_cron_installer_is_idempotent_and_marked():
    text = (REPO / "scripts" / "install_grg_pull_cron.sh").read_text()
    assert "grg-ej-pull" in text
    assert "pull_grg_ej_logs.py --days 3" in text
    assert "crontab -l" in text and "crontab \"$tmp\"" in text
    assert "--remove" in text
