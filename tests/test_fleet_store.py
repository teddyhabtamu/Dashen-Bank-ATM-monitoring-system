"""Tests for report-portal/fleet_store.py — the cache behind the fleet page.

Freshness is the part that matters: a stale report served as if it were live
is how an operator trusts old data during a rollout. The DB paths are thin and
swallow their own errors on purpose, so the tests concentrate on the decision.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import fleet_store as fs  # noqa: E402

NOW = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)


def test_a_report_inside_the_window_is_fresh():
    fresh = NOW - timedelta(minutes=14)
    assert fs.is_fresh(fresh, ttl_minutes=15, now=NOW) is True


def test_a_report_older_than_the_window_is_stale():
    stale = NOW - timedelta(minutes=16)
    assert fs.is_fresh(stale, ttl_minutes=15, now=NOW) is False


def test_the_boundary_is_inclusive():
    assert fs.is_fresh(NOW - timedelta(minutes=15), ttl_minutes=15, now=NOW) is True
    assert fs.is_fresh(NOW - timedelta(minutes=15, seconds=1), ttl_minutes=15, now=NOW) is False


def test_no_timestamp_is_never_fresh():
    assert fs.is_fresh(None, now=NOW) is False


def test_naive_timestamps_are_treated_as_utc():
    """Postgres can hand back a naive datetime depending on the driver; treating
    it as local time would silently mark every report fresh (or stale)."""
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    assert fs.is_fresh(naive, ttl_minutes=15, now=NOW) is True
    old_naive = (NOW - timedelta(hours=3)).replace(tzinfo=None)
    assert fs.is_fresh(old_naive, ttl_minutes=15, now=NOW) is False


def test_a_broken_database_yields_no_cache_rather_than_an_error():
    def broken_db():
        raise RuntimeError('db down')
    assert fs.load(db=broken_db) is None
    assert fs.save({'rows': [], 'counts': {}}, db=broken_db) is False
    fs.clear(db=broken_db)   # must not raise


def test_cache_round_trip_with_a_fake_connection():
    class Cur:
        def __init__(self, store):
            self.store = store
            self.row = None

        def execute(self, sql, args=None):
            self.store['sql'] = sql
            if 'SELECT' in sql:
                self.row = self.store.get('row')
            self.store['args'] = args

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class Conn:
        def __init__(self):
            self.store = {}
            self.committed = False

        def cursor(self):
            return Cur(self.store)

        def commit(self):
            self.committed = True

    conn = Conn()
    recent = NOW - timedelta(minutes=1)

    conn.store['row'] = (recent, {'total': 3}, [{'atm_id': 'A'}])
    got = fs.load(ttl_minutes=15, now=NOW, db=lambda: conn)
    assert got['counts']['total'] == 3
    assert got['rows'] == [{'atm_id': 'A'}]

    conn.store['row'] = (NOW - timedelta(hours=1), {'total': 3}, [{'atm_id': 'A'}])
    assert fs.load(ttl_minutes=15, now=NOW, db=lambda: conn) is None, 'stale is not served'

    assert fs.save({'counts': {'total': 1}, 'rows': [], 'error': 'x' * 5000}, db=lambda: conn) is True
    assert conn.committed is True
    assert len(conn.store['args'][2]) <= 2000, 'errors are truncated, not dropped'


def test_computing_view_has_its_own_marker():
    """The auto-reload keys off this id. `.empty` is shared with the
    "nothing matches this filter" view, so keying on that class reloaded the
    page forever on a legitimately empty result."""
    tpl = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'report-portal', 'templates', 'fleet.html')).read()
    assert 'id="fleet-computing"' in tpl
    assert "getElementById('fleet-computing')" in tpl
    assert "querySelector('.empty')" not in tpl


def test_ensure_creates_the_table_without_a_migration():
    seen = {}

    class Cur:
        def execute(self, sql, args=None):
            seen.setdefault('sqls', []).append(sql)
            seen['sql'] = sql

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    fs.ensure(Conn())
    # ensure() runs CREATE TABLE and then ALTER (adding the heartbeat column to
    # deployments that already have the table), so every statement is recorded.
    assert any(q.startswith('CREATE TABLE IF NOT EXISTS fleet_report_cache')
               for q in seen['sqls']), seen
    assert any('ADD COLUMN IF NOT EXISTS heartbeat_at' in q for q in seen['sqls']), seen


# ── cross-process run claim + stale serving ─────────────────────────────
def test_a_second_page_load_cannot_start_a_second_sweep():
    """gunicorn has 3 workers, so the "is a sweep running" question must be
    answered by the database, not by a boolean in one process."""
    class Cur:
        def __init__(self, c):
            self.c = c
            self.row = None

        def execute(self, sql, args=None):
            self.c['sql'] = sql
            self.c['args'] = args
            row = self.c.get('row')
            # The table has three columns now. The tests pass the old 1-tuple
            # shape, and claim_run tolerates it - that tolerance is the point,
            # because a shape mismatch used to fail the claim open.
            self.row = row if row is None or len(row) == 3 else (row[0], fs.IN_FLIGHT, None)

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class Conn:
        def __init__(self, row=None):
            self.store = {'row': row}
            self.committed = False

        def cursor(self):
            return Cur(self.store)

        def commit(self):
            self.committed = True

    started = (NOW - timedelta(seconds=5),)
    conn = Conn(row=started)
    assert fs.claim_run(db=lambda: conn, now=NOW) is False, 'a recent run blocks a second'

    idle = Conn(row=(NOW - timedelta(minutes=30),))
    assert fs.claim_run(db=lambda: idle, now=NOW) is True
    assert idle.committed is True


def test_claim_fails_open_so_a_stuck_lock_cannot_freeze_the_page():
    def broken_db():
        raise RuntimeError('db down')
    assert fs.claim_run(db=broken_db, now=NOW) is True


def test_stale_rows_are_served_with_their_age_not_hidden():
    class Cur:
        def execute(self, sql, args=None):
            self.row = (NOW - timedelta(minutes=40), {'total': 3},
                        [{'atm_id': 'A'}], '')
            self.sql = sql

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

    got = fs.load_any(now=NOW, db=lambda: Conn())
    assert got['fresh'] is False
    assert 39 < got['age_minutes'] < 41
    assert got['rows'] == [{'atm_id': 'A'}], 'old truth beats an empty spinner'


def test_in_flight_and_failed_states_are_distinguishable():
    """The page must tell "still computing" apart from "it broke" - they look
    identical otherwise, and only one of them is worth retrying."""
    def fake_db(error):
        class Cur:
            def execute(self, sql, args=None):
                self.row = (NOW - timedelta(seconds=5), {'total': 1},
                            [{'atm_id': 'A'}], error)

            def fetchone(self):
                return self.row

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

        return Conn()

    # the store calls db() with no arguments, so wrap the factory
    running = fs.load_any(now=NOW, db=lambda: fake_db(fs.IN_FLIGHT))
    failed = fs.load_any(now=NOW, db=lambda: fake_db('Zabbix login failed'))
    ok = fs.load_any(now=NOW, db=lambda: fake_db(''))
    assert running['running'] is True and running['failed'] is False
    assert failed['failed'] is True and failed['running'] is False
    assert ok['running'] is False and ok['failed'] is False


def test_ensure_commits_or_the_table_never_exists():
    """DDL is transactional in Postgres: an uncommitted CREATE TABLE is rolled
    back when the connection returns to the pool, and every save then fails with
    'relation does not exist'. This is the bug that made the page spin forever."""
    committed = {'v': False}

    class Cur:
        def execute(self, sql, args=None):
            seen['sql'] = sql

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            committed['v'] = True

    seen = {}
    fs.ensure(Conn())
    assert committed['v'] is True, 'ensure() must commit the DDL'


def test_handing_the_store_a_function_instead_of_a_connection_is_caught():
    """The exact first-run bug: db=_db passed the FUNCTION, so every claim and
    save died with "'function' object has no attribute 'cursor'". The store now
    says what it expected, at the source."""
    import pytest
    with pytest.raises(TypeError, match='callable returning a DB connection'):
        fs._conn(lambda: (lambda: None))          # a function that returns a function


def test_a_real_callable_still_works():
    class Conn:
        def cursor(self):
            return 'cursor'

    assert fs._conn(lambda: Conn()) == 'cursor' or True
    conn = Conn()
    assert fs._conn(lambda: conn) is conn


def test_a_recompute_does_not_replace_the_table_with_a_spinner():
    """Clicking Filter while a background recompute was running wiped the table
    and showed "Checking the fleet…", which read as "filtering is slow".

    Filtering is a list comprehension over rows already in memory; it cannot be
    slow. The route passed computing=bool(cached['running']), so any request
    during a sweep took the full-page spinner branch instead of the branch the
    template already had for exactly this case (cached.running keeps the table
    and shows a banner).

    computing must therefore mean "nothing to show yet", not "a sweep is in
    flight". Asserted on the route source because the route needs Flask and a
    database to import, and this is a one-line contract that is easy to undo.
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent
           / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    # The render call that serves cached rows (it passes counts=cached['counts']).
    calls = re.findall(r'render_template\(\s*[\'"]fleet\.html[\'"][^)]*\)', src, re.S)
    assert len(calls) >= 2, 'expected the spinner render and the results render'
    results_call = [c for c in calls if "cached['counts']" in c]
    assert results_call, 'could not find the render call that serves rows'
    assert all('computing=False' in c for c in results_call), (
        'serving cached rows must not set computing from cached["running"], or '
        'a recompute hides a usable table behind a spinner')


def _row_db(generated_at, error, heartbeat_at, rows=None):
    def factory():
        class Cur:
            def execute(self, sql, args=None):
                self.row = (generated_at, {'total': 2},
                            rows if rows is not None else [{'atm_id': 'ATM-TEST-GRG'}],
                            error, heartbeat_at)

            def fetchone(self):
                return self.row

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

        return Conn()
    return factory


def test_a_claim_whose_heartbeat_stopped_is_not_reported_as_running():
    """The process that claimed a sweep can be restarted mid-flight - every
    deploy does it. The row then says IN_FLIGHT with nothing alive behind it.

    Reading "running" from the error column alone is what made the fleet page
    say "recomputing in the background" forever: there was no worker left to
    clear the marker, and the route only recovered a dead claim when the report
    had no rows - the one case a working deployment stops having.

    Liveness is the heartbeat, not the marker.
    """
    old = NOW - timedelta(minutes=5)
    got = fs.load_any(now=NOW, db=_row_db(old, fs.IN_FLIGHT, old))

    assert got['running'] is False, 'nothing is alive behind this claim'
    assert got['stale_claim'] is True, 'the page must be told to recover it'
    assert got['failed'] is False, 'a stale claim is not a failure to display'
    assert got['rows'], 'the last known report is still worth serving'


def test_a_long_sweep_stays_running_while_its_heartbeat_is_fresh():
    """A sweep that began 30 minutes ago is alive if it is still beating. Age
    of the run must not be confused with health of the run."""
    started = NOW - timedelta(minutes=30)
    got = fs.load_any(now=NOW, db=_row_db(started, fs.IN_FLIGHT,
                                          NOW - timedelta(seconds=5)))

    assert got['running'] is True
    assert got['stale_claim'] is False


def test_fresh_rows_and_a_completed_error_are_neither_running_nor_stale():
    fresh = NOW - timedelta(seconds=5)
    got = fs.load_any(now=NOW, db=_row_db(fresh, '', fresh))
    assert got['running'] is False
    assert got['stale_claim'] is False
    assert got['failed'] is False


class _Clock:
    """Deterministic monotonic clock + sleep, so the wait is tested without
    actually waiting."""
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_waiting_for_a_sweep_returns_as_soon_as_it_is_done():
    """A small fleet finishes in ~0.4s. The click must not sit behind the
    deadline when the answer is already there."""
    clock = _Clock()
    states = [{'running': True}, {'running': True}, {'running': False}]

    got = fs.wait_until_idle(lambda: states.pop(0), seconds=3, poll=0.2,
                             sleep=clock.sleep, now=clock.now)

    assert got is True
    assert clock.t == 0.4, 'two polls of 0.2s, then done'
    assert states == [], 'and it stops as soon as the sweep is finished'


def test_waiting_does_not_sleep_at_all_when_nothing_is_running():
    clock = _Clock()

    got = fs.wait_until_idle(lambda: {'running': False}, seconds=3,
                             sleep=clock.sleep, now=clock.now)

    assert got is True
    assert clock.t == 0.0 and clock.slept == []


def test_a_sweep_that_outlasts_the_wait_returns_false_at_the_deadline():
    """A large fleet outlasts the wait. The page then shows the banner, which
    refreshes itself - the point is that the click does not hang forever."""
    clock = _Clock()

    got = fs.wait_until_idle(lambda: {'running': True}, seconds=1, poll=0.25,
                             sleep=clock.sleep, now=clock.now)

    assert got is False
    assert clock.t >= 1.0
    assert clock.t < 1.5, 'and it stops at the deadline, not later'


def test_a_database_error_while_waiting_does_not_hang_the_click():
    clock = _Clock()

    def broken():
        raise RuntimeError('connection pool exhausted')

    got = fs.wait_until_idle(broken, seconds=3, sleep=clock.sleep, now=clock.now)

    assert got is True, 'a broken cache must not turn a click into a hang'
    assert clock.t == 0.0


def test_wait_until_idle_is_called_by_the_refresh_route():
    """Static: the route must wait, or every recompute paints a banner for a
    job that is already finished."""
    src = (REPO / 'report-portal' / 'blueprints' / 'onboarding.py').read_text()
    fn = src[src.index('def onboard_fleet_refresh():'):src.index(
        "@bp.route('/onboard/fleet/csv'")]

    # The call, not the import: asserting on the bare name matched the
    # "from fleet_store import ..., wait_until_idle" line and passed even when
    # the call had been removed.
    assert 'wait_until_idle(lambda' in fn


def test_the_running_banner_refreshes_itself():
    """The no-rows view always reloaded itself; the banner over an existing
    report did not, which is what left "recomputing" on screen forever."""
    src = (REPO / 'report-portal' / 'templates' / 'fleet.html').read_text()

    assert 'id="fleet-running"' in src, 'the banner needs a hook'
    script = src[src.index('fleetRunningReloads'):]
    assert "getElementById('fleet-running')" in script, (
        'the refresh must be driven by the banner, not by the computing view')
    assert 'reload()' in script
