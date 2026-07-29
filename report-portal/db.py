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

    def close(self):
        try:
            object.__getattribute__(self, '_pool').putconn(
                object.__getattribute__(self, '_conn'))
        except Exception as e:
            logger.error('Failed to return connection to pool: %s', e)

    def __getattr__(self, name):
        if name in ('_conn', '_pool'):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        if name in ('_conn', '_pool'):
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
