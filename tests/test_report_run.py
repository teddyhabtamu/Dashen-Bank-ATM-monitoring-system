"""Execution contract for a validated report.

The compiler can be right and the run still wrong: totals must not be a sum of
pre-averaged rows, truncation must be reported, and a totals failure must not
turn a usable report into a 500.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'report-portal'))

import report_engine as engine  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_quality_cache():
    """The data-quality cache is process-wide; tests must not leak counts."""
    engine._QUALITY_CACHE.clear()
    yield
    engine._QUALITY_CACHE.clear()


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if sql.startswith('SET LOCAL'):
            self._rows = []
            self._one = None
            return
        if 'FROM atm_locations' in sql and 'COUNT(*)' in sql and 'SELECT COUNT(*) FROM atm_locations WHERE' in sql:
            self._one = (0,)
            self._rows = [(0,)]
            return
        if 'GROUP BY' in sql:
            self._rows = list(self.conn.main_rows)
            self._one = self.conn.main_rows[0] if self.conn.main_rows else None
            return
        # Totals query.
        if self.conn.totals_error:
            raise RuntimeError('totals exploded')
        self._one = self.conn.totals_row
        self._rows = [self.conn.totals_row] if self.conn.totals_row else []
        return

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def close(self):
        self.conn.closed += 1


class _Conn:
    def __init__(self, main_rows, totals_row, totals_error=False):
        self.main_rows = main_rows
        self.totals_row = totals_row
        self.totals_error = totals_error
        self.executed = []
        self.closed = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1


def _definition(limit=500):
    return engine.validate_definition({
        'dataset': 'transactions',
        'group_by': ['district'],
        'measures': ['txn_count', 'success_rate'],
        'filters': {'date': {'preset': 'last_7_days'}},
        'limit': limit,
    }, max_limit=engine.MAX_EXPORT_LIMIT)


def test_run_report_formats_rows_and_computes_totals_separately():
    conn = _Conn(
        main_rows=[('Bole', 120, 97.5), ('Kazanchis', 80, 93.75)],
        totals_row=(200, 96.0),
    )
    result = engine.run_report(conn, _definition(), limit=500)

    assert result['columns'][0]['id'] == 'district'
    assert result['rows'] == [['Bole', 120, 97.5],
                              ['Kazanchis', 80, 93.75]]
    assert result['totals'] == [None, 200, 96.0]
    assert result['row_count'] == 2
    assert result['limit'] == 500
    assert result['truncated'] is False
    assert result['generated_at'].endswith('EAT')
    # The totals query is a second statement, not a Python sum of 97.5 and
    # 93.75 (which would be 95.625, not 96.0).
    assert len([s for s, _ in conn.executed if 'GROUP BY' in s]) == 1


def test_truncation_is_reported_not_hidden():
    conn = _Conn(
        main_rows=[('A', 1, 100.0), ('B', 2, 100.0), ('C', 3, 100.0)],
        totals_row=(6, 100.0),
    )
    result = engine.run_report(conn, _definition(limit=2), limit=2)
    assert result['row_count'] == 2
    assert result['rows'] == [['A', 1, 100.0], ['B', 2, 100.0]]
    assert result['truncated'] is True
    assert result['limit'] == 2


def test_a_failed_totals_query_does_not_lose_the_rows():
    conn = _Conn(
        main_rows=[('Bole', 120, 97.5)],
        totals_row=None,
        totals_error=True,
    )
    result = engine.run_report(conn, _definition(), limit=500)
    assert result['rows'] == [['Bole', 120, 97.5]]
    assert result['totals'] is None
    assert conn.rollbacks == 1


def test_a_statement_timeout_becomes_an_actionable_error():
    class _TimeoutCursor(_Cursor):
        def execute(self, sql, params=None):
            if sql.startswith('SET LOCAL'):
                return
            raise RuntimeError('canceling statement due to statement timeout')

    class _TimeoutConn(_Conn):
        def cursor(self):
            return _TimeoutCursor(self)

    with pytest.raises(engine.ReportExecutionError) as exc:
        engine.run_report(_TimeoutConn([], None), _definition())
    assert 'narrow the period' in str(exc.value).lower()


def test_warnings_explain_missing_location_data():
    class _WarningCursor(_Cursor):
        def execute(self, sql, params=None):
            self.conn.executed.append((sql, params))
            if sql.startswith('SET LOCAL'):
                return
            if 'GROUP BY' in sql:
                self._rows = list(self.conn.main_rows)
                self._one = self.conn.main_rows[0]
                return
            if 'FROM atm_locations' in sql:
                self._one = (3,)
                self._rows = [(3,)]
                return
            self._one = self.conn.totals_row
            self._rows = [self.conn.totals_row]

    class _WarningConn(_Conn):
        def cursor(self):
            return _WarningCursor(self)

    result = engine.run_report(
        _WarningConn([('Unassigned', 10, 90.0)], (10, 90.0)),
        _definition(), limit=500)
    assert any('3 active ATMs have no district' in w
               for w in result['warnings'])
