"""Tests for report-portal/db.py connection-pool hygiene.

Written after /admin/onboard/fleet answered in ~5010ms on every view with

    fleet cache table unavailable: canceling statement due to lock timeout

5010ms is exactly the connection's lock_timeout. The cause was not in the fleet
code: psycopg2's pool does not reset connections, and get_db() opens a
transaction just by issuing SET statement_timeout / SET lock_timeout. The
connection was returned to the pool idle-in-transaction, holding the locks of
whatever it had read - so the next request's CREATE/ALTER TABLE ...
IF NOT EXISTS, which needs an ACCESS EXCLUSIVE lock, waited the full timeout.

The same leak meant uncommitted work from one request could be committed by an
unrelated later request on the same pooled connection. A rollback on return is
the fix for both.

psycopg2 is not installed in the test environment (the suite must run without a
database or its driver), so the import is stubbed. The wrapper under test is
pure Python.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))


def _import_db():
    """Import db.py with a stand-in psycopg2.pool."""
    fake_psycopg2 = types.ModuleType('psycopg2')
    fake_pool = types.ModuleType('psycopg2.pool')

    class PoolError(Exception):
        pass

    fake_pool.PoolError = PoolError
    fake_pool.ThreadedConnectionPool = object
    fake_psycopg2.pool = fake_pool
    sys.modules['psycopg2'] = fake_psycopg2
    sys.modules['psycopg2.pool'] = fake_pool
    sys.modules.pop('db', None)
    try:
        import db
    finally:
        # Leave the interpreter as we found it: the db module keeps its own
        # reference to the stand-in, so it keeps working, but no other test
        # can accidentally import the fake driver.
        sys.modules.pop('psycopg2', None)
        sys.modules.pop('psycopg2.pool', None)
    return db


class FakeConn:
    def __init__(self, closed=0, rollback_raises=False):
        self.closed = closed
        self.events = []
        self._rollback_raises = rollback_raises

    def rollback(self):
        self.events.append('rollback')
        if self._rollback_raises:
            raise RuntimeError('server closed the connection unexpectedly')

    def commit(self):
        self.events.append('commit')

    def cursor(self):
        return 'cursor'


class FakePool:
    def __init__(self):
        self.events = []
        self.put = []

    def putconn(self, conn):
        self.events.append('putconn')
        self.put.append(conn)


def test_returning_a_connection_to_the_pool_rolls_it_back_first():
    """The order matters: rollback, then putconn. A rollback after putconn
    would act on a connection another thread may already hold."""
    db = _import_db()
    conn, pool = FakeConn(), FakePool()
    wrapper = db._PooledConnection(conn, pool)

    wrapper.close()

    assert conn.events == ['rollback'], 'the transaction must be reset'
    assert pool.events == ['putconn']
    assert pool.put == [conn], 'the same connection goes back to the pool'


def test_a_closed_connection_is_not_rolled_back():
    """Calling rollback on a dead connection raises; the wrapper must still
    return it so the pool can account for it."""
    db = _import_db()
    conn, pool = FakeConn(closed=2), FakePool()

    db._PooledConnection(conn, pool).close()

    assert conn.events == []
    assert pool.events == ['putconn']


def test_a_failed_rollback_still_returns_the_connection():
    """Losing the connection permanently would exhaust the pool on a server
    that just went away. Log and return it anyway."""
    db = _import_db()
    conn, pool = FakeConn(rollback_raises=True), FakePool()

    db._PooledConnection(conn, pool).close()

    assert conn.events == ['rollback']
    assert pool.events == ['putconn']


def test_the_wrapper_still_delegates_and_returns_itself_from_context():
    """The wrapper is used as `with get_db() as conn:`, and callers reach
    straight through it for cursor() and commit(). A change to close() must not
    break either."""
    db = _import_db()
    conn, pool = FakeConn(), FakePool()
    wrapper = db._PooledConnection(conn, pool)

    assert wrapper.__enter__() is wrapper
    wrapper.commit()          # delegated to the real connection
    assert hasattr(wrapper, 'cursor')
    with wrapper as got:
        assert got is wrapper

def test_a_connection_left_to_the_garbage_collector_is_returned():
    """fleet_store.py takes a connection in every function and never closes it.
    Without a finalizer each of those connections is lost for good: psycopg2's
    pool tracks in-use connections per thread and never reclaims them, so every
    background sweep thread permanently consumed a slot. At DB_POOL_MAX the pool
    raises, save() and record_failure() both swallow that, and the sweep's claim
    stays marked in flight with nobody left to clear it - a permanent
    "recomputing in the background"."""
    import gc
    db = _import_db()
    conn, pool = FakeConn(), FakePool()

    wrapper = db._PooledConnection(conn, pool)
    del wrapper
    gc.collect()

    assert pool.events == ['putconn'], 'the connection must come back on its own'
    assert pool.put == [conn]


def test_closing_twice_returns_the_connection_only_once():
    """__del__ calls close(), so every normal `with` block now runs close()
    twice. Putting the same connection back twice would leave it in the pool
    while still in use, and hand it to two threads at once."""
    db = _import_db()
    conn, pool = FakeConn(), FakePool()
    wrapper = db._PooledConnection(conn, pool)

    wrapper.close()
    wrapper.close()

    assert pool.events == ['putconn'], 'putconn must be called exactly once'


def test_a_finalized_wrapper_is_safe_during_interpreter_shutdown():
    """__del__ runs while modules are being torn down and must never raise."""
    db = _import_db()
    conn, pool = FakeConn(rollback_raises=True), FakePool()

    wrapper = db._PooledConnection(conn, pool)
    wrapper.__del__()          # must not raise

    assert pool.events == ['putconn']
