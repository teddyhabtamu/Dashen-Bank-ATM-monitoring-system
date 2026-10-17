"""Runs tests/test_split_verify.sh against a real postgres, when one is available.

The verify steps in split_portal_db.sh had produced two confidently wrong
verdicts - a perfect copy reported BEHIND because Zabbix's own sequences were in
the comparison, and an unreachable database reported "All tables match" - and
neither was catchable from Python, because the logic is shell and needs an
actual pg_dump to mean anything.

So this starts a throwaway postgres cluster, builds a source database shaped like
the real one (portal tables with serial columns, in the same database as
Zabbix's tables), and drives the shipped functions against it.

It skips when no postgres server is installed. That is deliberate rather than
aspirational: on the UAT box the check ran only after a live split had already
produced a false alarm, because there was no way to rehearse it first.
"""
import glob
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
HARNESS = REPO / 'tests' / 'test_split_verify.sh'


def find_pg_bin():
    """Locate a postgres server install, or None."""
    if shutil.which('initdb') and shutil.which('postgres'):
        return pathlib.Path(shutil.which('initdb')).parent
    for cand in sorted(glob.glob('/usr/lib/postgresql/*/bin'), reverse=True):
        if os.access(os.path.join(cand, 'initdb'), os.X_OK) and \
           os.access(os.path.join(cand, 'postgres'), os.X_OK):
            return pathlib.Path(cand)
    return None


@pytest.fixture(scope='module')
def pg_cluster():
    pgbin = find_pg_bin()
    if pgbin is None:
        pytest.skip('no postgres server installed; cannot rehearse the split verify')
    tmp = tempfile.mkdtemp(prefix='pgtest-')
    data = os.path.join(tmp, 'data')
    port = '55439'
    try:
        r = subprocess.run([str(pgbin / 'initdb'), '-D', data, '-U', 'zabbix',
                            '--auth=trust'], capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            pytest.skip('initdb failed: %s' % r.stderr[-300:])
        subprocess.run([str(pgbin / 'pg_ctl'), '-D', data, '-o',
                        '-p %s -k %s' % (port, tmp), '-l',
                        os.path.join(tmp, 'pg.log'), 'start'],
                       capture_output=True, timeout=120)
        # Wait for it to accept connections rather than sleeping blindly.
        for _ in range(60):
            r = subprocess.run([str(pgbin / 'psql'), '-X', '-tAc', 'select 1',
                                '-h', tmp, '-p', port, '-U', 'zabbix', '-d', 'postgres'],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                break
            __import__('time').sleep(0.5)
        else:
            pytest.skip('postgres did not start')
        yield (str(pgbin), port, tmp)
    finally:
        subprocess.run([str(pgbin / 'pg_ctl'), '-D', data, '-m', 'immediate', 'stop'],
                       capture_output=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_steps_against_a_real_postgres(pg_cluster):
    pgbin, port, sockdir = pg_cluster
    # The harness writes its table list under /tmp/opencode, matching where it is
    # normally run; give it a directory of its own so a parallel run cannot
    # collide with anything.
    os.makedirs('/tmp/opencode', exist_ok=True)
    r = subprocess.run(['bash', str(HARNESS), pgbin, port, sockdir],
                       capture_output=True, text=True, timeout=900)
    print(r.stdout)
    assert r.returncode == 0, 'verify harness failed:\n%s\n%s' % (r.stdout, r.stderr)
    assert '0 failed' in r.stdout, r.stdout