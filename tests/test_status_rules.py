"""Dependency-free unit tests for zabbix_modules/status_rules.py.

interpret_state decides every ATM's displayed state (portal live badge +
state-manager writes). The WMI branch is load-bearing since 2026-10-12:
NCR agent hosts must read IN_SERVICE/HARDWARE_FAULT from part states while
their SNMP keys are unmapped simulator roots.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from zabbix_modules.status_rules import interpret_state, wmi_part_errors, wmi_device_unavail, part_display


def test_wmi_error_surfaces_fault():
    m = {'atm.parts': '{...}',
         'atm.part.part_status[Transport (2)]': '2',
         'atm.part.part_status[Card Reader]': '0'}
    assert interpret_state('NCR', m) == 'HARDWARE_FAULT'
    assert wmi_part_errors(m) == ['Transport (2)']


def test_wmi_ignores_dead_snmp():
    m = {'atm.parts': '{...}',
         'atm.part.part_status[Card Reader]': '0',
         'atm.status': None, 'atm.net.link': None,
         'atm.cash.cassette1': None}
    assert interpret_state('NCR', m) == 'IN_SERVICE'


def test_wmi_unknown_is_not_fault():
    # Cassettes sit at 4 (Unknown) on unconfigured cores — not actionable.
    m = {'atm.parts': '{...}',
         'atm.part.part_status[First Cassette]': '4'}
    assert interpret_state('NCR', m) == 'IN_SERVICE'
    assert wmi_part_errors(m) == []


def test_empty_stays_unreachable():
    assert interpret_state('NCR', {}) == 'UNREACHABLE'
    assert interpret_state('NCR', None) == 'UNREACHABLE'


def test_device_unavailable_is_fault():
    m = {'atm.parts': '{...}',
         'atm.device.status[RPNT-01-UsbThermal]': '2'}
    assert interpret_state('NCR', m) == 'HARDWARE_FAULT'
    assert wmi_device_unavail(m) == ['RPNT-01-UsbThermal']


def test_cash_cluster_unavailable_is_out_of_service():
    m = {'atm.parts': '{...}',
         'atm.device.status[CASH-01-UsbMediaDispenser]': '2'}
    assert interpret_state('NCR', m) == 'OUT_OF_SERVICE'


def test_device_warning_does_not_fault():
    m = {'atm.parts': '{...}',
         'atm.device.status[CASH-01-UsbMediaDispenser]': '1'}
    assert interpret_state('NCR', m) == 'IN_SERVICE'


def test_composite_empty_beats_unknown():
    # Paper today: part 4 (Unknown) + replenishment 2 (Error) reads Empty.
    assert part_display(4, 2) == ('Empty', 'error')


def test_composite_low_beats_unknown():
    # Cassettes today: part 4 (Unknown) + replenishment 1 (Warning).
    assert part_display(4, 1) == ('Low', 'warn')


def test_composite_quiet_pairs_stay_unknown():
    # Bins at 4+0: non-actionable either way — grey, never red.
    assert part_display(4, 0) == ('Unknown', 'unknown')
    assert part_display(4, None) == ('Unknown', 'unknown')


def test_composite_error_dominates():
    assert part_display(2, 2) == ('Error', 'error')
    assert part_display(2, 0) == ('Error', 'error')
    assert part_display(0, 0) == ('OK', 'ok')
    assert part_display(1, 0) == ('Warning', 'warn')
    assert part_display(0, 1) == ('Low', 'warn')
    assert part_display(None, None) == ('Unknown', 'unknown')


def test_grg_xfs_host_reads_in_service():
    m = {
        'atm.xfs.cdm': '{}',
        'atm.xfs.cdm.device_status': '0',
        'atm.xfs.cdm.total_notes': '3140',
        'atm.xfs.cdm.cassette.count[0050A]': '100',
        'atm.xfs.cdm.cassette.count[0100A]': '100',
    }
    assert interpret_state('GRG', m) == 'IN_SERVICE'


def test_grg_xfs_host_with_no_disposable_notes_is_out_of_cash():
    m = {
        'atm.xfs.cdm': '{}',
        'atm.xfs.cdm.device_status': '0',
        'atm.xfs.cdm.total_notes': '0',
        'atm.xfs.cdm.cassette.count[0050A]': '0',
        'atm.xfs.cdm.cassette.count[0100A]': '0',
    }
    assert interpret_state('GRG', m) == 'OUT_OF_CASH'


def test_snmp_only_host_unchanged():
    # GRG has no WMI keys — legacy SNMP path must behave exactly as before.
    assert interpret_state('NCR', {'atm.status': '1', 'atm.net.link': '1',
                                   'atm.cash.cassette1': '100'}) == 'IN_SERVICE'
    assert interpret_state('NCR', {'atm.status': None, 'atm.net.link': None,
                                   'atm.cash.cassette1': None}) == 'UNREACHABLE'
    assert interpret_state('NCR', {'atm.status': '2', 'atm.net.link': '1',
                                   'atm.cash.cassette1': '100'}) == 'OUT_OF_SERVICE'
