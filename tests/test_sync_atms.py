"""Dependency-free unit tests for scripts/sync_atms_to_zabbix.py pure logic.

Bulk day stands on this file (1,200 hosts, one loop, no resume) — the
CSV-str vs DB-int crash proved hand-testing isn't enough. Everything here
imports stdlib-only and touches no network, no DB, no Zabbix.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import sync_atms_to_zabbix as sync


def test_snmp_version_from_db_int():
    assert sync.parse_snmp_version(2) == 2
    assert sync.parse_snmp_version(3) == 3


def test_snmp_version_from_csv_str():
    assert sync.parse_snmp_version('2') == 2
    assert sync.parse_snmp_version(' 3 ') == 3


def test_snmp_version_defaults():
    assert sync.parse_snmp_version('') == 2
    assert sync.parse_snmp_version(None) == 2
    assert sync.parse_snmp_version('v2c') == 2  # garbage never kills the loop


def test_host_key_prefers_atm_id():
    row = {'atm_id': 'ATM-1', 'terminal_id': 'T-1'}
    assert sync.resolve_host_key(row) == 'ATM-1'


def test_host_key_falls_back_to_terminal():
    assert sync.resolve_host_key({'atm_id': '', 'terminal_id': 'T-1'}) == 'T-1'
    assert sync.resolve_host_key({'atm_id': None, 'terminal_id': 'T-1'}) == 'T-1'


def test_host_key_blank_row():
    assert sync.resolve_host_key({}) == ''
    assert sync.resolve_host_key({'atm_id': None, 'terminal_id': None}) == ''


def test_ncr_gets_snmp_plus_wmi():
    tids, group = sync.select_templates('NCR', 'ncr-tid', 'grg-tid', 'wmi-tid')
    assert tids == ['ncr-tid', 'wmi-tid']
    assert group == 'ATM-NCR'


def test_ncr_without_wmi_template_still_imports():
    tids, group = sync.select_templates('NCR', 'ncr-tid', 'grg-tid', None)
    assert tids == ['ncr-tid']
    assert group == 'ATM-NCR'


def test_grg_snmp_only():
    tids, group = sync.select_templates('GRG', 'ncr-tid', 'grg-tid', 'wmi-tid')
    assert tids == ['grg-tid']
    assert group == 'ATM-GRG'


def test_grg_prefers_xfs_when_available():
    tids, group = sync.select_templates('GRG', 'ncr-tid', 'grg-tid', 'wmi-tid', 'grg-xfs-tid')
    assert tids == ['grg-xfs-tid']
    assert group == 'ATM-GRG'


def test_unknown_vendor_skips():
    assert sync.select_templates('HYOSUNG', 'n', 'g', 'w') == (None, None)
    assert sync.select_templates('', 'n', 'g', 'w') == (None, None)


def test_missing_vendor_template_skips():
    assert sync.select_templates('NCR', None, 'g', 'w') == (None, None)
    assert sync.select_templates('GRG', 'n', None, 'w') == (None, None)


def test_district_proxy_map_points_at_known_proxies():
    expected = {'zabbix-proxy-adama', 'zabbix-proxy-addis-east',
                'zabbix-proxy-addis-north', 'zabbix-proxy-addis-south',
                'zabbix-proxy-addis-west', 'zabbix-proxy-bahirdar',
                'zabbix-proxy-dessie', 'zabbix-proxy-hawassa'}
    assert set(sync.DISTRICT_PROXY.values()) <= expected
    assert len(sync.DISTRICT_PROXY) >= 8
