#!/usr/bin/env python3
"""AK3: fleet-scale indexes. Idempotent — safe to re-run on any environment.

Adds composite indexes the per-ATM time-window queries need at 1300-ATM
scale (fleet table, KPI ranking, fault lists, availability trend):

  idx_atm_txn_atm_recorded   ON atm_transactions (atm_id, recorded_at DESC)
  idx_state_hist_atm_started ON atm_state_history (atm_id, started_at DESC)

CONCURRENTLY = zero write locks (safe on the live UAT DB), hence autocommit
(audit.py-style startup migrations can't do this — CONCURRENTLY forbids
transaction blocks). IF NOT EXISTS = re-runnable.

Usage: python3 scripts/add_perf_indexes.py   (uses DB_HOST/DB_NAME/DB_USER/DB_PASS)
"""
import os, sys
import sys
import time

import psycopg2

INDEXES = [
    ("idx_atm_txn_atm_recorded",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_atm_txn_atm_recorded "
     "ON atm_transactions (atm_id, recorded_at DESC)"),
    ("idx_state_hist_atm_started",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_state_hist_atm_started "
     "ON atm_state_history (atm_id, started_at DESC)"),
]



def _require_db_name():
    """DB_NAME must be explicit. Do not guess.

    These scripts run inside a container (which sets DB_NAME correctly) or on a
    host (where it is unset). Defaulting to 'zabbix' meant that the standalone
    case quietly operated on the pre-split database - the one the portal no
    longer reads - and reported success. An unset variable now stops the script
    instead of sending the work somewhere harmless-looking and wrong.
    """
    name = os.environ.get('DB_NAME')
    if not name:
        sys.stderr.write(
            'DB_NAME is not set, so this cannot tell which database to work on.\n'
            'Run it inside a container that has it:\n'
            '    docker exec -i report-portal python3 - < scripts/add_perf_indexes.py\n'
            'or set DB_NAME explicitly. Refusing to assume.\n' % NAME)
        sys.exit(2)
    return name


def main():
    conn = psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        dbname=_require_db_name(),
        user=os.environ.get('DB_USER', 'zabbix'),
        password=os.environ.get('DB_PASS', ''),
        connect_timeout=10,
    )
    conn.autocommit = True  # required for CONCURRENTLY
    try:
        with conn.cursor() as cur:
            for name, ddl in INDEXES:
                t0 = time.time()
                cur.execute(ddl)
                print(f'{name}: ensured in {time.time() - t0:.1f}s')
            cur.execute(
                "SELECT indexname, pg_size_pretty(pg_relation_size(indexrelid)) "
                "FROM pg_stat_user_indexes WHERE schemaname = 'public' "
                "AND indexname IN ('idx_atm_txn_atm_recorded', 'idx_state_hist_atm_started') "
                "ORDER BY 1")
            for indexname, size in cur.fetchall():
                print(f'{indexname}: {size}')
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
