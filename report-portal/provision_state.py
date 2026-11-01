"""Durable Zabbix provisioning state — the outbox behind host creation.

Registration used to start a one-shot daemon thread. If Zabbix was down, the
thread logged and disappeared: the ATM row existed, the host did not, and
nothing retried. That is the failure this module removes.

The flow is deliberately small:

    save/import -> enqueue(atm_id) -> scheduler claims due rows -> provision
                -> record_outcomes() -> synced / retry / blocked

`blocked` is not a failure to retry: it means the registry cannot be
provisioned until a human changes it (no IP, unknown vendor). `retry` is a
transient failure (Zabbix unreachable, API error) with exponential backoff.
`synced` means the last write landed and the host is correct.

This table is portal-owned. It is not Zabbix data and it is safe to rebuild:
every row is derived from `atm_locations` plus the last provisioning attempt.
"""
from __future__ import annotations

import logging

from db import get_db

logger = logging.getLogger(__name__)

TABLE = 'atm_provision_state'

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    atm_id          VARCHAR(20) PRIMARY KEY,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_action     VARCHAR(20),
    last_error      TEXT,
    next_attempt_at TIMESTAMP,
    last_success_at TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
)
"""

INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_atm_provision_due "
    f"ON {TABLE} (status, next_attempt_at)"
)

# 60s, 2m, 4m, 8m, 16m, 32m, then 1h. A Zabbix outage longer than an hour is
# still retried; the row is never abandoned.
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
STALE_SYNC_SECONDS = 900

SYNCED_ACTIONS = frozenset(('create', 'update', 'skipped', 'reenabled',
                            'macros_fixed'))
BLOCKED_ACTIONS = frozenset(('no_ip', 'review', 'conflict'))


def init_provision_state_table():
    """Create the table and index. Never fatal: a fresh DB must still serve.

    Same savepoint pattern as agent_registry/audit: one failed DDL statement
    must not abort the rest of startup and leave the portal unable to log in.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            for statement in (TABLE_SQL, INDEX_SQL):
                try:
                    cur.execute("SAVEPOINT sp_provision")
                    cur.execute(statement)
                    cur.execute("RELEASE SAVEPOINT sp_provision")
                except Exception as exc:  # noqa: BLE001 - startup must continue
                    cur.execute("ROLLBACK TO SAVEPOINT sp_provision")
                    logger.warning('provision schema step skipped: %s', exc)
            conn.commit()
            cur.close()
        logger.info('%s table ready', TABLE)
    except Exception as exc:  # noqa: BLE001 - never block login on this
        logger.error('Failed to init %s: %s', TABLE, exc)


def backoff_seconds(attempts):
    """Delay before the next retry. `attempts` is the count already made.

    attempts=0 (first failure) -> 60s; 1 -> 120s; ... capped at one hour.
    Kept pure so the backoff is testable without a database.
    """
    try:
        n = max(0, int(attempts))
    except (TypeError, ValueError):
        n = 0
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** min(n, 6)))


def status_for_action(action, error=''):
    """Map a provisioner result to (durable status, human-readable message).

    A `skipped` result is a success: the host was already correct. A `failed`
    result is transient until proven otherwise; `no_ip` and `review` need a
    registry change and must not spin forever.
    """
    action = (action or 'failed').strip().lower()
    if action in SYNCED_ACTIONS:
        return 'synced', None
    if action in BLOCKED_ACTIONS:
        default = 'This ATM needs engineer review.'
        if action == 'no_ip':
            default = 'No IP address is recorded for this ATM.'
        elif action == 'conflict':
            default = ('A Zabbix host with the same visible name already '
                       'exists; rename one ATM or remove the stale row.')
        return 'blocked', (error or default)
    return 'retry', (error or 'Zabbix provisioning failed.')


def enqueue(atm_id, reason='save'):
    """Queue one ATM for provisioning. Idempotent and safe to call on every save.

    A row already `syncing` is left alone: resetting it would let a second
    worker start a duplicate host.create while the first is still in flight.
    The save's change is picked up by the next reconcile/save instead.
    """
    return enqueue_many([atm_id], reason=reason)


def enqueue_many(atm_ids, reason='save'):
    ids = [str(i).strip() for i in (atm_ids or []) if str(i).strip()]
    if not ids:
        return 0
    with get_db() as conn:
        cur = conn.cursor()
        for atm_id in ids:
            cur.execute(f"""
                INSERT INTO {TABLE}
                    (atm_id, status, attempts, next_attempt_at, updated_at)
                VALUES (%s, 'pending', 0, NOW(), NOW())
                ON CONFLICT (atm_id) DO UPDATE SET
                    status          = 'pending',
                    attempts        = 0,
                    next_attempt_at = NOW(),
                    last_error      = NULL,
                    updated_at      = NOW()
                WHERE {TABLE}.status <> 'syncing'
            """, (atm_id,))
        conn.commit()
        cur.close()
    logger.info('provision enqueue: %d ATM(s), reason=%s', len(ids), reason)
    return len(ids)


def _upsert_synced(cur, atm_id, action):
    cur.execute(f"""
        INSERT INTO {TABLE}
            (atm_id, status, attempts, last_action, last_error,
             next_attempt_at, last_success_at, updated_at)
        VALUES (%s, 'synced', 0, %s, NULL, NULL, NOW(), NOW())
        ON CONFLICT (atm_id) DO UPDATE SET
            status          = 'synced',
            attempts        = 0,
            last_action     = EXCLUDED.last_action,
            last_error      = NULL,
            next_attempt_at = NULL,
            last_success_at = NOW(),
            updated_at      = NOW()
    """, (atm_id, action))


def _upsert_blocked(cur, atm_id, action, message):
    cur.execute(f"""
        INSERT INTO {TABLE}
            (atm_id, status, attempts, last_action, last_error,
             next_attempt_at, updated_at)
        VALUES (%s, 'blocked', 0, %s, %s, NULL, NOW())
        ON CONFLICT (atm_id) DO UPDATE SET
            status          = 'blocked',
            attempts        = 0,
            last_action     = EXCLUDED.last_action,
            last_error      = EXCLUDED.last_error,
            next_attempt_at = NULL,
            updated_at      = NOW()
    """, (atm_id, action, message))


def _upsert_retry(cur, atm_id, action, message):
    """Insert/update a transient failure and schedule the next attempt.

    Existing rows use their stored attempt count for the backoff. The INSERT
    branch only applies to a manual provision of a row that was never
    enqueued, so a fresh row starts at 60 seconds.
    """
    cur.execute(f"""
        INSERT INTO {TABLE}
            (atm_id, status, attempts, last_action, last_error,
             next_attempt_at, updated_at)
        VALUES (%s, 'retry', 1, %s, %s, NOW() + INTERVAL '60 seconds', NOW())
        ON CONFLICT (atm_id) DO UPDATE SET
            status          = 'retry',
            attempts        = {TABLE}.attempts + 1,
            last_action     = EXCLUDED.last_action,
            last_error      = EXCLUDED.last_error,
            next_attempt_at = NOW() + (
                LEAST({RETRY_MAX_SECONDS},
                      {RETRY_BASE_SECONDS} * POWER(2, LEAST({TABLE}.attempts, 6)))::text
                || ' seconds')::interval,
            updated_at      = NOW()
    """, (atm_id, action, message))


def record_outcome(atm_id, action, error=''):
    """Persist one provisioner result. Used by provision.py and the worker."""
    atm_id = (atm_id or '').strip()
    if not atm_id:
        return
    status, message = status_for_action(action, error)
    with get_db() as conn:
        cur = conn.cursor()
        if status == 'synced':
            _upsert_synced(cur, atm_id, action)
        elif status == 'blocked':
            _upsert_blocked(cur, atm_id, action, message)
        else:
            _upsert_retry(cur, atm_id, action, message)
        conn.commit()
        cur.close()


def record_outcomes(results):
    """Persist a batch of provision_atms() results in one transaction."""
    rows = [r for r in (results or []) if (r or {}).get('atm_id')]
    if not rows:
        return
    with get_db() as conn:
        cur = conn.cursor()
        for result in rows:
            atm_id = str(result.get('atm_id')).strip()
            action = result.get('action') or 'failed'
            error = result.get('error') or ''
            status, message = status_for_action(action, error)
            if status == 'synced':
                _upsert_synced(cur, atm_id, action)
            elif status == 'blocked':
                _upsert_blocked(cur, atm_id, action, message)
            else:
                _upsert_retry(cur, atm_id, action, message)
        conn.commit()
        cur.close()


def record_failure(atm_ids, message):
    """Mark claimed rows as a transient failure after a worker-level exception."""
    ids = [str(i).strip() for i in (atm_ids or []) if str(i).strip()]
    if not ids:
        return
    with get_db() as conn:
        cur = conn.cursor()
        for atm_id in ids:
            _upsert_retry(cur, atm_id, 'failed', message or 'Provisioning worker failed.')
        conn.commit()
        cur.close()


def forget(atm_ids):
    """Drop state for ATMs that no longer exist (deleted between claim and run)."""
    ids = [str(i).strip() for i in (atm_ids or []) if str(i).strip()]
    if not ids:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {TABLE} WHERE atm_id = ANY(%s)", (ids,))
        conn.commit()
        cur.close()


def claim_due(limit=25):
    """Atomically claim due rows. Returns the ATM ids this worker now owns.

    FOR UPDATE SKIP LOCKED is what makes this safe even if a second worker
    starts (a rolling restart, a manual run, or a future second replica).
    Stale `syncing` rows are returned to `retry` first, so a killed worker
    cannot park an ATM forever.
    """
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 25
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE {TABLE}
               SET status = 'retry',
                   next_attempt_at = NOW(),
                   last_error = COALESCE(last_error,
                                         'Provisioning worker stopped mid-run.'),
                   updated_at = NOW()
             WHERE status = 'syncing'
               AND updated_at < NOW() - INTERVAL '{STALE_SYNC_SECONDS} seconds'
        """)
        cur.execute(f"""
            SELECT atm_id
              FROM {TABLE}
             WHERE status IN ('pending', 'retry')
               AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
             ORDER BY next_attempt_at ASC NULLS FIRST, atm_id
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        """, (limit,))
        ids = [row[0] for row in cur.fetchall()]
        if ids:
            cur.execute(f"""
                UPDATE {TABLE}
                   SET status = 'syncing', updated_at = NOW()
                 WHERE atm_id = ANY(%s)
            """, (ids,))
        conn.commit()
        cur.close()
    return ids


def run_due(limit=25):
    """Claim and provision due ATMs. Never raises; returns a small summary.

    The actual write goes through provision.provision_atms(), so this worker
    and the registration/wizard paths share one implementation. provision_atms
    records the per-ATM outcome; this function only handles failures that
    happen before it can.
    """
    ids = claim_due(limit)
    if not ids:
        return {'claimed': 0, 'summary': {}, 'error': ''}
    try:
        from provision import provision_atms
        out = provision_atms(ids, dry_run=False, actor='provision-worker')
        if out.get('error') and not out.get('results'):
            if 'No matching ATM records' in out['error']:
                forget(ids)
            else:
                record_failure(ids, out['error'])
        return {
            'claimed': len(ids),
            'summary': out.get('summary') or {},
            'error': out.get('error') or '',
        }
    except Exception as exc:  # noqa: BLE001 - a worker must never kill the scheduler
        logger.warning('provision worker failed for %s: %s', ids, exc)
        record_failure(ids, str(exc))
        return {'claimed': len(ids), 'summary': {}, 'error': str(exc)}


def retry_now(atm_id):
    """Make one ATM due immediately, creating the row if it never existed."""
    atm_id = (atm_id or '').strip()
    if not atm_id:
        return False
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO {TABLE}
                (atm_id, status, attempts, next_attempt_at, updated_at)
            VALUES (%s, 'pending', 0, NOW(), NOW())
            ON CONFLICT (atm_id) DO UPDATE SET
                status          = 'pending',
                next_attempt_at = NOW(),
                last_error      = NULL,
                updated_at      = NOW()
        """, (atm_id,))
        conn.commit()
        changed = cur.rowcount
        cur.close()
    return bool(changed)


def get(atm_id):
    """One ATM's state, or None. The Agent page renders this; it must not 500."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT atm_id, status, attempts, last_action, last_error,
                   next_attempt_at, last_success_at, updated_at
              FROM {TABLE} WHERE atm_id = %s
        """, (atm_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        cur.close()
    return dict(zip(cols, row)) if row else None


def status_map(atm_ids):
    """{atm_id: state_row} for table badges; empty on any read failure."""
    ids = [str(i).strip() for i in (atm_ids or []) if str(i).strip()]
    if not ids:
        return {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT atm_id, status, attempts, last_action, last_error,
                       next_attempt_at, last_success_at, updated_at
                  FROM {TABLE} WHERE atm_id = ANY(%s)
            """, (ids,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
        return {r[0]: dict(zip(cols, r)) for r in rows}
    except Exception as exc:  # noqa: BLE001 - a badge must not break a page
        logger.warning('provision status map failed: %s', exc)
        return {}


def summary():
    """Counts by status, for the fleet/onboarding view."""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY status")
            rows = cur.fetchall()
            cur.close()
        return {status: count for status, count in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning('provision summary failed: %s', exc)
        return {}
