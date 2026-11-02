"""The refresh diagnostic must use the same clock as the state manager.

UAT 2026-11-02: the queue query assumed ``items.state``.  Zabbix 6.4 keeps the
runtime fields in ``item_rtdata`` (or splits them between the two tables), so
the check failed while the human-readable report still looked plausible.  A
diagnostic that cannot read the queue is worse than none: it sends the
operator looking for an ATM fault when the pollers are simply behind.

These tests pin the schema detection and the freshness budget.  The budget is
deliberately shared with ``zabbix_modules.state_freshness``: a 1m item is
allowed 210s, not 120s, because two missed polls plus slack is already the
state manager's contract.
"""
import importlib.util
import pathlib
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    'refresh_state_items_under_test',
    REPO / 'scripts' / 'refresh_state_items.py')
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)

from zabbix_modules.state_freshness import source_freshness


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ''

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


def runtime(sql_rows):
    return refresh._runtime_columns(FakeCursor(sql_rows))


def test_budget_matches_the_state_manager():
    now = time.time()
    for delay in ('30s', '1m', '2m', '5m', '1h'):
        _age, allowed = source_freshness(
            {'atm.xfs.cdm': 'x'}, {'atm.xfs.cdm': now},
            {'atm.xfs.cdm': delay}, now=now)
        assert refresh.budget_seconds(delay) == allowed, delay


def test_within_budget_uses_the_item_delay():
    now = time.time()
    assert refresh.within_budget(
        {'lastclock': now - 147, 'delay': '1m'}, now)
    assert not refresh.within_budget(
        {'lastclock': now - 211, 'delay': '1m'}, now)
    assert not refresh.within_budget(
        {'lastclock': None, 'delay': '1m'}, now)


def test_runtime_columns_all_on_items():
    join_sql, state, nextcheck, lastclock, error = runtime([
        ('state', 'items'), ('nextcheck', 'items'),
        ('lastclock', 'items'), ('error', 'items'),
    ])
    assert join_sql == ''
    assert (state, nextcheck, lastclock, error) == (
        'i.state', 'i.nextcheck', 'i.lastclock', 'i.error')


def test_runtime_columns_all_on_item_rtdata():
    join_sql, state, nextcheck, lastclock, error = runtime([
        ('state', 'item_rtdata'), ('nextcheck', 'item_rtdata'),
        ('lastclock', 'item_rtdata'), ('error', 'item_rtdata'),
    ])
    assert 'item_rtdata' in join_sql
    assert (state, nextcheck, lastclock, error) == (
        'r.state', 'r.nextcheck', 'r.lastclock', 'r.error')


def test_runtime_columns_handles_a_split_layout():
    join_sql, state, nextcheck, lastclock, error = runtime([
        ('state', 'items'), ('nextcheck', 'item_rtdata'),
        ('lastclock', 'items'), ('error', 'items'),
    ])
    assert 'item_rtdata' in join_sql
    assert state == 'i.state'
    assert nextcheck == 'r.nextcheck'
    assert lastclock == 'i.lastclock'


def test_runtime_columns_prefers_the_runtime_table_when_duplicated():
    _join_sql, state, nextcheck, _lastclock, _error = runtime([
        ('state', 'items'), ('state', 'item_rtdata'),
        ('nextcheck', 'items'), ('nextcheck', 'item_rtdata'),
        ('lastclock', 'items'), ('lastclock', 'item_rtdata'),
    ])
    assert state == 'r.state'
    assert nextcheck == 'r.nextcheck'


def test_runtime_columns_reports_what_is_missing():
    try:
        runtime([('state', 'items')])
    except RuntimeError as exc:
        assert 'nextcheck' in str(exc)
        assert 'lastclock' in str(exc)
    else:
        raise AssertionError('a partial schema must fail loudly')
