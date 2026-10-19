"""A fleet sweep must never be declared dead while it is demonstrably alive.

The claim in fleet_store is a single row shared by 3 gunicorn workers, and its
window was originally 3 minutes - sized for the OLD implementation, where a
sweep took minutes. Two problems followed once batching made a sweep take
seconds:

  * a worker recycled mid-sweep left the page saying "checking the fleet" for
    three minutes for a job that normally finishes in two seconds;
  * worse, if a sweep ever legitimately ran longer than the window (a slow
    Zabbix server, a large fleet), a page load would see the claim as expired
    and start a SECOND sweep over the same cache row.

Liveness is a heartbeat question, not a start-time question. These tests pin
that, and pin the failure direction: if the heartbeat cannot be read for any
reason, the claim must NOT fail open, because a duplicated fleet sweep costs
far more than a slightly stale report.
"""
import datetime
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import fleet_store as fs  # noqa: E402

NOW = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)


def _ago(minutes):
    return NOW - datetime.timedelta(minutes=minutes)


class FakeCur:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def execute(self, sql, args=None):
        self.conn.data['sql'] = sql
        self.conn.data['args'] = args
        if sql.startswith('SELECT generated_at, error, heartbeat_at'):
            self.row = self.conn.data.get('row')

    def fetchone(self):
        return self.row

    def close(self):
        pass


class FakeConn:
    def __init__(self, row=None, boom=False):
        self.data = {'row': row}
        self.committed = False
        self.boom = boom

    def cursor(self):
        if self.boom:
            raise RuntimeError('relation does not exist')
        return FakeCur(self)

    def commit(self):
        self.committed = True


# ── the race ───────────────────────────────────────────────────────────────

def test_a_live_sweep_blocks_a_second_one_even_when_it_is_old():
    """Started 10 minutes ago, but heartbeating 5 seconds ago.

    This is the case a start-time window gets wrong: the sweep is slow, not dead,
    and a second one must not start over the same cache row.
    """
    row = (_ago(10), fs.IN_FLIGHT, _ago(0.008))
    assert fs.claim_run(db=lambda: FakeConn(row), window_minutes=1, now=NOW) is False, (
        'a sweep with a recent heartbeat is alive regardless of when it started')


def test_a_dead_sweep_is_reclaimed():
    """Started recently, but the heartbeat stopped. The worker died."""
    row = (_ago(0.1), fs.IN_FLIGHT, _ago(5))
    assert fs.claim_run(db=lambda: FakeConn(row), window_minutes=1, now=NOW) is True


def test_a_pre_heartbeat_row_still_blocks_a_second_sweep():
    """A deployment upgraded mid-claim has NULL heartbeat_at. Falling back to
    generated_at keeps the original guarantee instead of allowing duplicates."""
    row = (_ago(0.2), fs.IN_FLIGHT, None)
    assert fs.claim_run(db=lambda: FakeConn(row), window_minutes=1, now=NOW) is False


def test_a_completed_report_never_blocks_a_fresh_sweep():
    """error is not IN_FLIGHT, so it is a result, not a run in progress."""
    row = (_ago(0.1), '', _ago(0.1))
    assert fs.claim_run(db=lambda: FakeConn(row), window_minutes=1, now=NOW) is True


def test_a_short_row_does_not_fail_the_claim_open():
    """Regression guard.

    claim_run catches every exception and returns True ("fail open"), which is
    the right call for a database that is merely unavailable and catastrophic
    for a row whose shape changed. Unpacking is defensive so the two cannot be
    confused: if it raises here, two workers sweep the fleet at once.
    """
    for row in ((_ago(0.2),), (_ago(0.2), fs.IN_FLIGHT), (_ago(0.2), fs.IN_FLIGHT, _ago(0.2))):
        got = fs.claim_run(db=lambda: FakeConn(row), window_minutes=1, now=NOW)
        assert got is False, 'row %r let a second sweep through' % (row,)


def test_heartbeat_touch_is_scoped_to_the_in_flight_row():
    """A heartbeat must not resurrect a finished report."""
    src = (REPO / 'report-portal' / 'fleet_store.py').read_text()
    fn = src[src.index('def touch_heartbeat'):src.index('def claim_run')]
    assert 'WHERE id = 1 AND error = %s' in fn, (
        'the heartbeat must only apply to a row still marked in flight')


def test_window_is_sized_for_the_batched_sweep_not_the_old_one():
    """Three minutes was right when a sweep took minutes. A seconds-long sweep
    must not hold the page for three times its own runtime."""
    assert fs.RUN_WINDOW_MINUTES <= 2, (
        'window is %s minutes; a batched sweep completes in seconds'
        % fs.RUN_WINDOW_MINUTES)


def test_window_is_tunable_without_a_redeploy():
    """A slower environment must be able to widen it via env."""
    assert 'FLEET_RUN_WINDOW_MINUTES' in (REPO / 'report-portal' / 'fleet_store.py').read_text()


# ── the heartbeat is actually sent ──────────────────────────────────────────

def test_fleet_reports_liveness_at_every_phase_boundary():
    """Three boundaries: after the batched Zabbix sweep, after the per-ATM
    verdicts, and after the provisioning dry run. The dry run is the slowest
    phase, so a heartbeat before it is the one that matters most."""
    src = (REPO / 'report-portal' / 'verify.py').read_text()
    body = src[src.index('def fleet('):]
    assert 'heartbeat=None' in body
    assert body.count('beat()') >= 3, (
        'expected a heartbeat at each phase boundary, found %d' % body.count('beat()'))


def test_compute_fleet_passes_a_real_heartbeat():
    src = (REPO / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    fn = src[src.index('def _compute_fleet'):src.index("@bp.route('/onboard/fleet'")]
    assert 'touch_heartbeat' in fn
    assert 'heartbeat=lambda' in fn, (
        'the callback must be wired, not merely imported')


def test_a_heartbeat_failure_cannot_abort_the_sweep():
    """Best-effort. Losing one heartbeat must not throw away a minutes of work."""
    src = (REPO / 'report-portal' / 'verify.py').read_text()
    body = src[src.index('def fleet('):]
    assert 'except Exception as e:' in body[body.index('def beat():'):body.index('def beat():') + 400]

def test_a_stale_claim_is_recovered_even_when_a_report_exists():
    """The recovery condition used to require an empty report. That is the one
    state a deployment stops having after its first good sweep, so every
    restart mid-sweep left the banner up permanently."""
    src = (REPO / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    fn = src[src.index('def onboard_fleet():'):]

    start = fn.index("if cached and cached.get('stale_claim')")
    end = fn.index('cached = load_any', start)
    recovery = fn[start:end]

    assert 'stale_claim' in fn, 'the route must act on a dead claim'
    assert 'rows' not in recovery, (
        'recovery must not be gated on having no rows: a deployment that has '
        'ever produced a report always has rows, which is exactly when a '
        'restart mid-sweep leaves the banner up forever')


def test_the_worker_has_no_in_process_latch():
    """One lock, in the database. The boolean that used to live here could be
    held forever by a wedged thread, and every later claim then started a
    worker that returned immediately - leaving the claim set with no one to
    clear it."""
    src = (REPO / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    fn = src[src.index('def _compute_fleet'):src.index("@bp.route('/onboard/fleet'")]

    assert '_FLEET_RUNNING' not in fn
    assert 'if _FLEET_RUNNING' not in fn
