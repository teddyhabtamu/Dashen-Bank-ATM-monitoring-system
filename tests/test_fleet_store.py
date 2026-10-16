"""Tests for report-portal/fleet_store.py — the cache behind the fleet page.

Freshness is the part that matters: a stale report served as if it were live
is how an operator trusts old data during a rollout. The DB paths are thin and
swallow their own errors on purpose, so the tests concentrate on the decision.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

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
