"""Dependency-free unit tests for zabbix_modules/status_rules.py.

interpret_state decides every ATM's displayed state (portal live badge +
state-manager writes). The WMI branch is load-bearing since 2026-10-12:
NCR agent hosts must read IN_SERVICE/HARDWARE_FAULT from part states while
their SNMP keys are unmapped simulator roots.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from zabbix_modules.status_rules import interpret_state, wmi_part_errors


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


def test_snmp_only_host_unchanged():
    # GRG has no WMI keys — legacy SNMP path must behave exactly as before.
    assert interpret_state('NCR', {'atm.status': '1', 'atm.net.link': '1',
                                   'atm.cash.cassette1': '100'}) == 'IN_SERVICE'
    assert interpret_state('NCR', {'atm.status': None, 'atm.net.link': None,
                                   'atm.cash.cassette1': None}) == 'UNREACHABLE'
    assert interpret_state('NCR', {'atm.status': '2', 'atm.net.link': '1',
                                   'atm.cash.cassette1': '100'}) == 'OUT_OF_SERVICE'
