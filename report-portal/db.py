"""
db.py — PostgreSQL connection pool.
Uses psycopg2's ThreadedConnectionPool so Flask's threaded workers
reuse connections instead of opening a new one per request.

Returns a thin wrapper whose .close() returns the connection to the pool.
"""
import os
import logging
import threading
import time
from psycopg2 import pool

logger = logging.getLogger(__name__)

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')
DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN', 5))
DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX', 30))

_config = {
    'host': DB_HOST,
    'port': DB_PORT,
    'dbname': DB_NAME,
    'user': DB_USER,
    'password': DB_PASS,
}

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = pool.ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, **_config)
                    logger.info('DB pool created (min=%d, max=%d)', DB_POOL_MIN, DB_POOL_MAX)
                except Exception as e:
                    logger.critical('Failed to create DB pool: %s', e)
                    raise
    return _pool


class _PooledConnection:
    """Thin wrapper that delegates all attribute access to the real connection
    but overrides .close() to return it to the pool instead."""

    def __init__(self, conn, pool):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_pool', pool)
        object.__setattr__(self, '_returned', False)

    def close(self):
        """Return the connection to the pool, reset first, exactly once.

        psycopg2's pool does not reset state, and get_db() opens a transaction
        just by issuing SET statement_timeout / SET lock_timeout. Returning
        that connection as-is left it idle-in-transaction, holding every lock
        its queries had taken until some later borrower happened to commit.

        That is not theoretical. On the fleet page it took the form of
        fleet_store.ensure() - a CREATE/ALTER - waiting on lock_timeout (5s) on
        every single view, so /admin/onboard/fleet always answered in ~5010ms
        and the background sweep could never write its result. A rollback here
        is a no-op for a caller that already committed, and the correct reset
        for one that did not.

        Idempotent because __del__ also calls it: closing twice must not put
        the same connection into the pool twice, which would hand one
        connection to two threads at once.
        """
        if object.__getattribute__(self, '_returned'):
            return
        object.__setattr__(self, '_returned', True)
        conn = object.__getattribute__(self, '_conn')
        pool = object.__getattribute__(self, '_pool')
        try:
            if not getattr(conn, 'closed', 0):
                conn.rollback()
        except Exception as e:
            logger.error('Rollback before returning connection failed: %s', e)
        try:
            pool.putconn(conn)
        except Exception as e:
            logger.error('Failed to return connection to pool: %s', e)

    def __del__(self):
        """Return the connection even when a caller forgets to.

        fleet_store.py takes a connection in every one of its functions
        (claim_run, load_any, save, record_failure, touch_heartbeat) and never
        closes it. psycopg2's pool tracks in-use connections per THREAD, so
        each background sweep thread held a slot permanently. Once DB_POOL_MAX
        was reached, get_db() raised PoolError - and save()/record_failure()
        both swallow it, so the sweep's claim stayed marked in flight with
        nobody left to clear it. That is a silent, permanent "recomputing".

        Must never raise: __del__ runs during interpreter shutdown, where
        module globals such as logger may already be gone.
        """
        try:
            self.close()
        except Exception:
            pass

    def __getattr__(self, name):
        if name in ('_conn', '_pool', '_returned'):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        if name in ('_conn', '_pool', '_returned'):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, '_conn'), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def get_db():
    p = _get_pool()
    try:
        raw = p.getconn()
    except pool.PoolError as e:
        logger.warning('Connection pool exhausted, waiting 1s and retrying: %s', e)
        time.sleep(1)
        raw = p.getconn()
    conn = _PooledConnection(raw, p)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '30s'")
    cur.execute("SET lock_timeout = '5s'")
    cur.close()
    return conn



