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
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

TABLE = 'fleet_report_cache'
DEFAULT_TTL_MINUTES = 15


def _conn(db):
    """Call db() and insist it returned a connection.

    Passing the *function* instead of a connection produced
    "'function' object has no attribute 'cursor'" from three different places
    at once. This turns that into one clear message at the source.
    """
    conn = db()
    if not hasattr(conn, 'cursor'):
        raise TypeError(
            'db must be a callable returning a DB connection, e.g. db=_db_conn '
            f'(got {type(conn).__name__})')
    return conn


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
    # Added by ALTER rather than only to the CREATE: existing deployments already
    # have the table, and CREATE TABLE IF NOT EXISTS would never add it.
    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
    cur.close()
    conn.commit()
    return conn


IN_FLIGHT = '__running__'
# Sized against the measured sweep, not the old implementation. Before the
# batched rewrite a fleet sweep took minutes, so 3 minutes of exclusivity was
# about right. It now completes in seconds, which makes a 3-minute window two
# problems at once:
#
#   * a worker recycled mid-sweep leaves the page saying "checking" for 3 minutes
#     for a job that normally takes 2 seconds;
#   * and if a sweep ever legitimately runs longer than the window (a slow
#     Zabbix server, a large fleet), a page load sees the claim as expired and
#     starts a SECOND sweep over the same cache row.
#
# The heartbeat below fixes the second one properly; this value only governs how
# long we wait before declaring a run dead. Tunable for a slower environment
# without a redeploy.
RUN_WINDOW_MINUTES = int(os.environ.get('FLEET_RUN_WINDOW_MINUTES', '1'))


def touch_heartbeat(db=None):
    """Mark the in-flight sweep as alive. Cheap; safe to call often."""
    try:
        conn = _conn(db)
        cur = conn.cursor()
        cur.execute(f"UPDATE {TABLE} SET heartbeat_at = NOW() WHERE id = 1 AND error = %s",
                    (IN_FLIGHT,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.warning('fleet heartbeat failed: %s', e)
        return False


def claim_run(db=None, window_minutes=RUN_WINDOW_MINUTES, now=None):
    """Cross-process lock for the background sweep.

    A page load must not start a second full-fleet sweep while one is already
    running: gunicorn has 3 workers, so a boolean in memory proves nothing.
    The claim is a row write, which every worker can see. Marks the run and
    returns True; returns False when a sweep started recently.
    """
    try:
        conn = _conn(db)
        cur = conn.cursor()
        cur.execute(f"SELECT generated_at, error, heartbeat_at FROM {TABLE} WHERE id = 1")
        row = cur.fetchone()
        if row:
            # Tolerate a row that predates the heartbeat column rather than
            # unpacking blindly. A shape mismatch here would raise, be swallowed
            # by the except below, and return True - i.e. fail OPEN and let a
            # second sweep start. Duplicating a fleet sweep is far worse than
            # serving a slightly stale report.
            generated_at = row[0]
            error = row[1] if len(row) > 1 else IN_FLIGHT
            heartbeat_at = row[2] if len(row) > 2 else None
            # A run still marked in flight is only genuinely running if its
            # heartbeat is recent. Liveness is a heartbeat question, not a
            # start-time question: a slow sweep must not be declared dead
            # because it began more than a minute ago.
            last_seen = heartbeat_at or generated_at
            if error == IN_FLIGHT and is_fresh(last_seen, window_minutes, now):
                cur.close()
                return False
        cur.execute(
            f"INSERT INTO {TABLE} (id, generated_at, counts, rows, error, heartbeat_at) "
            f"VALUES (1, NOW(), '{{}}'::jsonb, '[]'::jsonb, %s, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET generated_at = NOW(), "
            "error = EXCLUDED.error, heartbeat_at = NOW()",
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
        conn = _conn(db)
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
        conn = _conn(db)
        cur = conn.cursor()
        cur.execute(f"UPDATE {TABLE} SET error = %s WHERE id = 1", (str(error)[:2000],))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning('fleet failure record failed: %s', e)


def load(ttl_minutes=DEFAULT_TTL_MINUTES, now=None, db=None):
    """Cached report if present and fresh, else None."""
    try:
        conn = _conn(db)
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
        conn = _conn(db)
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
