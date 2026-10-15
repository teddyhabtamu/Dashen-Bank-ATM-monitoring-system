"""report-portal/fleet_store.py — persisted cache for the fleet report.

Why a cache at all: verifying 1,202 ATMs is one Zabbix sweep per ATM. Doing it
inside a page request would take minutes and time out, so the report is
computed in the background and served from here.

Why in the DATABASE and not memory: gunicorn runs 3 workers, each with its own
process. An in-process cache would mean three different answers depending on
which worker answered, and a report that changes between refreshes is worse
than no report. One row in Postgres is shared by every worker.

Table is created on demand with CREATE TABLE IF NOT EXISTS, the same pattern
blueprints/admin.py already uses for atm_delete_archive - no migration step, no
downtime, and safe to run from several processes at once.
"""

import json
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))

TABLE = 'fleet_report_cache'
DEFAULT_TTL_MINUTES = 15


def is_fresh(generated_at, ttl_minutes=DEFAULT_TTL_MINUTES, now=None):
    """Pure: is this cached report still current? Tested directly.

    A report older than the TTL is stale even if it is complete: during a
    rollout the question changes minute to minute.
    """
    if not generated_at:
        return False
    now = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return (now - generated_at) <= timedelta(minutes=ttl_minutes)


def ensure(conn):
    conn.cursor().execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "id INT PRIMARY KEY DEFAULT 1,"
        "generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "counts JSONB NOT NULL DEFAULT '{}'::jsonb,"
        "rows JSONB NOT NULL DEFAULT '[]'::jsonb,"
        "error TEXT DEFAULT '')")
    return conn


def load(ttl_minutes=DEFAULT_TTL_MINUTES, now=None, db=None):
    """Cached report if present and fresh, else None."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(f"SELECT generated_at, counts, rows FROM {TABLE} WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        generated_at, counts, rows = row
        if not is_fresh(generated_at, ttl_minutes, now):
            return None
        return {'generated_at': generated_at.isoformat(),
                'counts': counts or {},
                'rows': rows or []}
    except Exception:
        return None


def save(payload, db=None):
    """Persist a report. Never raises: a failed cache write must not fail the
    report that was just computed."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO {TABLE} (id, generated_at, counts, rows, error) "
            "VALUES (1, NOW(), %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), counts = EXCLUDED.counts, "
            "rows = EXCLUDED.rows, error = EXCLUDED.error",
            (json.dumps(payload.get('counts') or {}),
             json.dumps(payload.get('rows') or []),
             (payload.get('error') or '')[:2000]))
        conn.commit()
        cur.close()
        return True
    except Exception:
        return False


def clear(db=None):
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {TABLE} WHERE id = 1")
        conn.commit()
        cur.close()
    except Exception:
        pass
