"""The provisioning outbox must not turn a transient failure into silence.

These tests pin the two decisions that matter without needing PostgreSQL:
what a provisioner result means, and how long the worker waits before retrying.
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import provision_state as ps  # noqa: E402


def test_a_created_or_skipped_host_is_synced():
    for action in ('create', 'update', 'skipped', 'reenabled', 'macros_fixed'):
        status, message = ps.status_for_action(action)
        assert status == 'synced', action
        assert message is None


def test_no_ip_review_and_conflict_are_blocked_not_retried():
    for action in ('no_ip', 'review', 'conflict'):
        status, message = ps.status_for_action(action, 'registry needs a fix')
        assert status == 'blocked'
        assert message == 'registry needs a fix'


def test_a_failed_write_is_retried_with_a_message():
    status, message = ps.status_for_action('failed', 'Zabbix API timeout')
    assert status == 'retry'
    assert message == 'Zabbix API timeout'
    assert ps.status_for_action('failed')[1]


def test_backoff_starts_at_one_minute_and_caps_at_one_hour():
    assert ps.backoff_seconds(0) == 60
    assert ps.backoff_seconds(1) == 120
    assert ps.backoff_seconds(2) == 240
    assert ps.backoff_seconds(6) == 3600
    assert ps.backoff_seconds(99) == 3600
    assert ps.backoff_seconds('bad') == 60


def test_the_state_table_carries_everything_the_worker_needs():
    sql = ps.TABLE_SQL
    for column in ('atm_id', 'status', 'attempts', 'last_action', 'last_error',
                   'next_attempt_at', 'last_success_at', 'updated_at'):
        assert column in sql, column
    assert ps.INDEX_SQL


def test_claim_is_atomic_across_workers():
    """The worker must claim with SKIP LOCKED, not SELECT-then-UPDATE.

    A rolling restart or a manual pass can overlap the scheduler; without the
    lock two workers would both try host.create for the same ATM.
    """
    source = (REPO / 'report-portal' / 'provision_state.py').read_text()
    assert 'FOR UPDATE SKIP LOCKED' in source
    assert "status = 'syncing'" in source
    assert "status IN ('pending', 'retry')" in source
