"""Persistence for saved custom reports.

The engine owns validation and SQL.  This module owns only the small amount of
state that makes a report reusable: its name, owner, visibility and the
normalized definition JSON.

Why a separate table instead of a row in ``scheduled_reports``
---------------------------------------------------------------
A saved report is not a schedule.  A user may want to reopen a report without
emailing it, and a schedule may later pin one definition version.  Keeping the
definition in its own table lets us add scheduling with a nullable FK rather
than overload the legacy ``report_type``/``params`` strings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json

from db import get_db

logger = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3))

REPORT_DEFINITIONS_SQL = """
CREATE TABLE IF NOT EXISTS report_definitions (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(120) NOT NULL,
    description TEXT DEFAULT '',
    dataset     VARCHAR(40) NOT NULL,
    definition  JSONB NOT NULL,
    owner       VARCHAR(100) NOT NULL,
    visibility  VARCHAR(10) NOT NULL DEFAULT 'private',
    is_system   BOOLEAN NOT NULL DEFAULT FALSE,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_def_owner
    ON report_definitions(owner, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_def_visibility
    ON report_definitions(visibility, updated_at DESC);
"""


_SCHEMA_LOCK = 999888780


def init_report_schema():
    """Create the definitions table.  Best-effort, like the other startup DDL.

    Gunicorn starts several workers at once.  Without the transaction-scoped
    advisory lock, two ``CREATE INDEX IF NOT EXISTS`` statements can race and
    one worker logs a duplicate-relation error even though the schema is fine.
    The lock is released by the commit, so a pooled connection never carries
    it into another request.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
                cur.execute(REPORT_DEFINITIONS_SQL)
                conn.commit()
            finally:
                cur.close()
        logger.info('report_definitions table ready')
    except Exception as exc:
        logger.error('Failed to init report_definitions: %s', exc)


def _to_eat(value, fmt='%Y-%m-%d %H:%M'):
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(EAT).strftime(fmt)
    return str(value)


def row_to_dict(row):
    """Convert a SELECT row to the JSON shape the builder consumes."""
    if not row:
        return None
    (definition_id, name, description, dataset, definition, owner,
     visibility, is_system, version, created_at, updated_at) = row
    if isinstance(definition, str):
        import json
        try:
            definition = json.loads(definition)
        except ValueError:
            definition = {}
    return {
        'id': definition_id,
        'name': name,
        'description': description or '',
        'dataset': dataset,
        'definition': definition or {},
        'owner': owner,
        'visibility': visibility,
        'is_system': bool(is_system),
        'version': version,
        'created_at': _to_eat(created_at),
        'updated_at': _to_eat(updated_at),
    }


_SELECT = """
    SELECT id, name, description, dataset, definition, owner, visibility,
           is_system, version, created_at, updated_at
    FROM report_definitions
"""


def list_definitions(conn, username, role):
    cur = conn.cursor()
    try:
        if role == 'admin':
            cur.execute(_SELECT + " ORDER BY is_system DESC, updated_at DESC")
        else:
            cur.execute(
                _SELECT + " WHERE owner = %s OR visibility = 'shared'"
                " ORDER BY is_system DESC, updated_at DESC",
                (username,))
        return [row_to_dict(row) for row in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:
            pass


def get_definition(conn, definition_id):
    cur = conn.cursor()
    try:
        cur.execute(_SELECT + " WHERE id = %s", (definition_id,))
        return row_to_dict(cur.fetchone())
    finally:
        try:
            cur.close()
        except Exception:
            pass


def create_definition(conn, *, name, description, dataset, payload,
                      owner, visibility='private'):
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO report_definitions "
            "(name, description, dataset, definition, owner, visibility) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id, name, description, dataset, definition, owner, "
            "          visibility, is_system, version, created_at, updated_at",
            (name, description or '', dataset, Json(payload), owner,
             visibility))
        row = cur.fetchone()
        conn.commit()
        return row_to_dict(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass


def update_definition(conn, definition_id, *, name, description, dataset,
                      payload, visibility):
    """Update a definition.  Caller has already passed the permission check."""
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE report_definitions SET name = %s, description = %s, "
            "dataset = %s, definition = %s, visibility = %s, "
            "version = version + 1, updated_at = NOW() "
            "WHERE id = %s AND is_system = FALSE "
            "RETURNING id, name, description, dataset, definition, owner, "
            "          visibility, is_system, version, created_at, updated_at",
            (name, description or '', dataset, Json(payload), visibility,
             definition_id))
        row = cur.fetchone()
        conn.commit()
        return row_to_dict(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass


def delete_definition(conn, definition_id):
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM report_definitions WHERE id = %s "
            "AND is_system = FALSE RETURNING id", (definition_id,))
        row = cur.fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
