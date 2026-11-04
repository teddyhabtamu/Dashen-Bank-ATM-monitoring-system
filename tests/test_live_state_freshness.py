"""The registry and dashboard must not call a stale snapshot "live".

The UAT contradiction: the ATM Registration chips counted the persisted state
(Agent Disconnected 2) while the rows and the distribution donut overrode it
with a hardware fault recomputed from 25-minute-old XFS values, and labelled
the row LIVE with "Last Seen Now".

`batch_fetch_live_states` now gates the overlay on the freshness of the values
that actually drive the state, using each item's own polling interval.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

from blueprints import admin  # noqa: E402
from zabbix_modules.state_freshness import freshness_budget_seconds  # noqa: E402

XFS_METRICS = {
    'atm.xfs.cdm': '{}',
    'atm.xfs.cdm.device_status': '0',
    'atm.xfs.cdm.cassette.count[0050A]': '100',
}


def snapshot(metrics, age_seconds, delay='1m'):
    now = int(time.time())
    return {'metrics': dict(metrics),
            'clocks': {k: now - age_seconds for k in metrics},
            'delays': {k: delay for k in metrics}}


def test_a_fresh_xfs_snapshot_is_current():
    assert admin.live_state_is_current(snapshot(XFS_METRICS, 30)) is True


def test_a_stale_xfs_snapshot_is_not_current():
    assert admin.live_state_is_current(snapshot(XFS_METRICS, 25 * 60)) is False


def test_a_five_minute_wmi_probe_is_not_dead_after_six_minutes():
    metrics = {'atm.parts': '{}', 'atm.part.part_status[Card Reader]': '0'}
    assert admin.live_state_is_current(snapshot(metrics, 6 * 60, delay='5m')) is True
    assert admin.live_state_is_current(snapshot(metrics, 15 * 60, delay='5m')) is False


def test_snmp_core_is_judged_on_its_own_cadence():
    metrics = {'atm.status': '1', 'atm.net.link': '1', 'atm.cash.cassette1': '100'}
    assert admin.live_state_is_current(snapshot(metrics, 60, delay='30s')) is True
    assert admin.live_state_is_current(snapshot(metrics, 10 * 60, delay='30s')) is False


def test_no_clocks_means_not_current():
    assert admin.live_state_is_current(
        {'metrics': XFS_METRICS, 'clocks': {}, 'delays': {}}) is False


def test_the_budget_is_two_missed_polls_plus_slack_and_is_capped():
    """One shared budget, so the state chip and the cash card agree on stale."""
    assert freshness_budget_seconds('30s') == 135
    assert freshness_budget_seconds('1m') == 210
    assert freshness_budget_seconds('5m') == 810
    assert freshness_budget_seconds('1h') == 900


def test_registry_overlay_skips_a_stale_snapshot(monkeypatch):
    monkeypatch.setattr(admin, 'ENABLE_SIMULATORS', False)
    monkeypatch.setattr(admin, 'fetch_zabbix_snapshot',
                        lambda atm_id: snapshot(XFS_METRICS, 25 * 60))
    out = admin.batch_fetch_live_states(
        [{'atm_id': 'ATM-TEST-GRG', 'vendor': 'GRG'}])
    assert out == {}, 'a stale snapshot must not overwrite the persisted state'


def test_registry_overlay_uses_a_fresh_snapshot(monkeypatch):
    monkeypatch.setattr(admin, 'ENABLE_SIMULATORS', False)
    monkeypatch.setattr(admin, 'fetch_zabbix_snapshot',
                        lambda atm_id: snapshot(XFS_METRICS, 30))
    out = admin.batch_fetch_live_states(
        [{'atm_id': 'ATM-TEST-GRG', 'vendor': 'GRG'}])
    assert out['ATM-TEST-GRG'][0] == 'IN_SERVICE'


def test_a_live_state_manager_check_wins_over_raw_recompute(monkeypatch):
    """The persisted state is already debounced by the state manager.

    Recomputing from one fresh-but-unconfirmed snapshot bypassed the
    confirmation window and made the badge flicker. While the state manager is
    reporting, the portal must leave its committed state alone.
    """
    monkeypatch.setattr(admin, 'ENABLE_SIMULATORS', False)
    monkeypatch.setattr(admin, 'fetch_zabbix_snapshot',
                        lambda atm_id: snapshot(XFS_METRICS, 30))
    out = admin.batch_fetch_live_states([
        {'atm_id': 'ATM-TEST-GRG', 'vendor': 'GRG',
         'state_checked_recently': True},
    ])
    assert out == {}, 'do not bypass the debounced persisted state'
