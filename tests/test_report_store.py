"""Persistence rules for saved report definitions.

The store is small, but it owns two things worth pinning: the JSONB payload is
always a dict (not a stringified blob), and a viewer's list never exposes
another user's private report.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pytest
from psycopg2.extras import Json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'report-portal'))

import report_store  # noqa: E402


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if 'RETURNING' in sql:
            self._one = self.conn.one
        elif 'SELECT id, name' in sql:
            self._all = list(self.conn.rows)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def close(self):
        self.conn.closed_cursors += 1


class _Conn:
    def __init__(self, rows=(), one=None):
        self.rows = rows
        self.one = one
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


PAYLOAD = {
    'dataset': 'transactions',
    'group_by': ['district'],
    'measures': ['txn_count'],
    'filters': {'date': {'preset': 'last_7_days'}, 'atm_scope': 'active'},
    'sort': {'id': 'txn_count', 'direction': 'desc'},
    'limit': 100000,
}

ROW = (1, 'Weekly volume', 'Monday pack', 'transactions', PAYLOAD, 'alice',
       'private', False, 2, dt.datetime(2026, 11, 1, 7, 30),
       dt.datetime(2026, 11, 6, 9, 0))


def test_create_definition_sends_a_jsonb_document_and_commits():
    conn = _Conn(one=ROW)
    result = report_store.create_definition(
        conn, name='Weekly volume', description='Monday pack',
        dataset='transactions', payload=PAYLOAD, owner='alice',
        visibility='private')

    sql, params = conn.executed[0]
    assert 'INSERT INTO report_definitions' in sql
    assert isinstance(params[3], Json)
    assert params[3].adapted == PAYLOAD
    assert params[4] == 'alice'
    assert conn.commits == 1
    assert result['id'] == 1
    assert result['definition'] == PAYLOAD


def test_update_definition_bumps_the_version_and_refuses_system_rows():
    conn = _Conn(one=ROW)
    report_store.update_definition(
        conn, 1, name='Renamed', description='', dataset='transactions',
        payload=PAYLOAD, visibility='shared')

    sql, params = conn.executed[0]
    assert 'version = version + 1' in sql
    assert "is_system = FALSE" in sql
    assert params[5] == 1


def test_a_viewer_list_is_scoped_to_owner_or_shared():
    conn = _Conn(rows=[ROW])
    rows = report_store.list_definitions(conn, 'alice', 'viewer')

    sql, params = conn.executed[0]
    assert "owner = %s OR visibility = 'shared'" in sql
    assert params == ('alice',)
    assert rows[0]['name'] == 'Weekly volume'


def test_an_admin_list_is_not_scoped():
    conn = _Conn(rows=[ROW])
    report_store.list_definitions(conn, 'admin', 'admin')

    sql, params = conn.executed[0]
    assert 'WHERE owner' not in sql
    assert params is None


def test_row_to_dict_parses_a_string_definition_from_an_old_row():
    row = list(ROW)
    row[4] = '{"dataset": "fleet", "measures": ["atm_count"]}'
    parsed = report_store.row_to_dict(tuple(row))
    assert parsed['definition']['dataset'] == 'fleet'


def test_delete_reports_when_nothing_was_removed():
    conn = _Conn(one=None)
    assert report_store.delete_definition(conn, 99) is False
    assert conn.commits == 1


def test_schema_init_takes_a_transaction_scoped_lock(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(report_store, 'get_db', lambda: conn)
    report_store.init_report_schema()

    first_sql, first_params = conn.executed[0]
    assert 'pg_advisory_xact_lock' in first_sql
    assert first_params == (report_store._SCHEMA_LOCK,)
    assert 'CREATE TABLE IF NOT EXISTS report_definitions' in conn.executed[1][0]
    assert conn.commits == 1


def test_schema_failure_is_logged_and_does_not_stop_startup(monkeypatch, caplog):
    def boom():
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(report_store, 'get_db', boom)
    report_store.init_report_schema()
    assert 'Failed to init report_definitions' in caplog.text
