"""Persistence for observed ATM identity.

Why a side table instead of new `atm_locations` columns: that table is
load-bearing. Its column list, `COALESCE` upserts and CSV import are shared by
the registration UI, the bulk importer and the provisioning path. Identity is
high-churn evidence ("what the box said five minutes ago"), not registry data,
so it belongs beside the row and must never overwrite it.

`atm_locations.image_roots` is the one column added here: an allow-list is an
approval, and approvals belong to the registry. The identity probe can only
*propose* a root; a human stores it here before it reaches a box.
"""
from __future__ import annotations

import logging

from psycopg2.extras import Json

from db import get_db

logger = logging.getLogger(__name__)

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS atm_agent_identity (
    atm_id          VARCHAR(20) PRIMARY KEY,
    reported_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    schema          INTEGER NOT NULL,
    payload         JSONB NOT NULL,
    terminal_id     VARCHAR(20),
    terminal_source VARCHAR(40),
    ej_hostname     VARCHAR(64),
    vendor          VARCHAR(20),
    probes          JSONB,
    conflicts       JSONB,
    received_by     VARCHAR(100)
)
"""

IMAGE_ROOTS_SQL = "ALTER TABLE atm_locations ADD COLUMN IF NOT EXISTS image_roots TEXT[]"


def init_agent_identity_table():
    """Create the side table and the one registry column. Never fatal.

    Same savepoint pattern as audit.init_audit_schema: a statement that cannot
    run (fresh DB, table owned by another service) must not abort the rest of
    startup and leave the portal unable to serve.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            for statement in (TABLE_SQL, IMAGE_ROOTS_SQL):
                try:
                    cur.execute("SAVEPOINT sp_agent")
                    cur.execute(statement)
                    cur.execute("RELEASE SAVEPOINT sp_agent")
                except Exception as exc:  # noqa: BLE001 - startup must continue
                    cur.execute("ROLLBACK TO SAVEPOINT sp_agent")
                    logger.warning('agent schema step skipped: %s', exc)
            conn.commit()
            cur.close()
        logger.info('atm_agent_identity table ready')
    except Exception as exc:  # noqa: BLE001 - never block login on this
        logger.error('Failed to init agent identity table: %s', exc)


def save_identity(atm_id, identity, conflicts, received_by=''):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO atm_agent_identity
                (atm_id, schema, payload, terminal_id, terminal_source,
                 ej_hostname, vendor, probes, conflicts, received_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (atm_id) DO UPDATE SET
                reported_at     = NOW(),
                schema          = EXCLUDED.schema,
                payload         = EXCLUDED.payload,
                terminal_id     = EXCLUDED.terminal_id,
                terminal_source = EXCLUDED.terminal_source,
                ej_hostname     = EXCLUDED.ej_hostname,
                vendor          = EXCLUDED.vendor,
                probes          = EXCLUDED.probes,
                conflicts       = EXCLUDED.conflicts,
                received_by     = EXCLUDED.received_by
        """, (
            atm_id,
            identity['schema'],
            Json(identity),
            identity.get('terminal_id'),
            identity.get('terminal_source'),
            (identity.get('ej') or {}).get('hostname'),
            identity.get('vendor'),
            Json(identity.get('probes') or {}),
            Json(conflicts or []),
            (received_by or '')[:100],
        ))
        conn.commit()
        cur.close()


def load_identity(atm_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT reported_at, schema, payload, terminal_id, terminal_source,
                   ej_hostname, vendor, probes, conflicts, received_by
            FROM atm_agent_identity WHERE atm_id = %s
        """, (atm_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        cur.close()
    if not row:
        return None
    return dict(zip(cols, row))


def image_roots(atm_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT image_roots FROM atm_locations WHERE atm_id = %s", (atm_id,))
        row = cur.fetchone()
        cur.close()
    return list(row[0] or []) if row else []


def set_image_roots(atm_id, roots):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE atm_locations SET image_roots = %s WHERE atm_id = %s",
                    (list(roots or []), atm_id))
        conn.commit()
        changed = cur.rowcount
        cur.close()
    return bool(changed)
