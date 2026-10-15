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
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

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
    """Create the cache table if absent. MUST commit: Postgres treats DDL as
    transactional, so an uncommitted CREATE TABLE is rolled back when the
    connection returns to the pool - and then every save() fails with
    "relation does not exist". That is exactly what happened on first deploy.
    """
    cur = conn.cursor()
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "id INT PRIMARY KEY DEFAULT 1,"
        "generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        "counts JSONB NOT NULL DEFAULT '{}'::jsonb,"
        "rows JSONB NOT NULL DEFAULT '[]'::jsonb,"
        "error TEXT DEFAULT '')")
    cur.close()
    conn.commit()
    return conn


IN_FLIGHT = '__running__'
RUN_WINDOW_MINUTES = 3


def claim_run(db=None, window_minutes=RUN_WINDOW_MINUTES, now=None):
    """Cross-process lock for the background sweep.

    A page load must not start a second full-fleet sweep while one is already
    running: gunicorn has 3 workers, so a boolean in memory proves nothing.
    The claim is a row write, which every worker can see. Marks the run and
    returns True; returns False when a sweep started recently.
    """
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(f"SELECT generated_at FROM {TABLE} WHERE id = 1")
        row = cur.fetchone()
        if row and is_fresh(row[0], window_minutes, now):
            cur.close()
            return False
        cur.execute(
            f"INSERT INTO {TABLE} (id, generated_at, counts, rows, error) "
            f"VALUES (1, NOW(), '{{}}'::jsonb, '[]'::jsonb, %s) "
            "ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), error = EXCLUDED.error",
            (IN_FLIGHT,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.warning('fleet run claim failed: %s', e)
        return True          # fail open: a stuck lock is worse than a slow page


def load_any(now=None, db=None):
    """Whatever is cached, fresh or not, plus how old it is.

    Serving the last known report with a "this is N minutes old" banner is far
    better than an empty spinner: an operator can act on stale truth and knows
    it is stale. Returns None only when nothing was ever computed.
    """
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(f"SELECT generated_at, counts, rows, error FROM {TABLE} WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        generated_at, counts, rows, error = row
        now = now or datetime.now(timezone.utc)
        ga = generated_at if getattr(generated_at, 'tzinfo', None) else \
            generated_at.replace(tzinfo=timezone.utc)
        age = (now - ga).total_seconds() / 60.0
        return {'generated_at': generated_at.isoformat(), 'counts': counts or {},
                'rows': rows or [], 'error': error or '', 'age_minutes': age,
                'running': (error == IN_FLIGHT),
                'failed': bool(error) and error != IN_FLIGHT,
                'fresh': age <= DEFAULT_TTL_MINUTES}
    except Exception:
        return None


def record_failure(error, db=None):
    """Store a failure so the page can show it instead of spinning forever."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(f"UPDATE {TABLE} SET error = %s WHERE id = 1", (str(error)[:2000],))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning('fleet failure record failed: %s', e)


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
    except Exception as e:
        # A swallowed write means the page spins forever with no explanation.
        # Say so loudly instead.
        logger.warning('fleet cache save failed: %s', e)
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
