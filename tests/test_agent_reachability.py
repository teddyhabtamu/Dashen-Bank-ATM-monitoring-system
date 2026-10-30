"""Agent reachability must not be confused with the device Network chip.

The image bridge already distinguishes an unreachable ATM from missing
evidence.  The detail page needs the same separation: the ``Network`` pill is
the ATM's own XFS/WMI link state, while agent reachability answers whether
Zabbix is still receiving data from the host.  These tests pin the freshness
rule and the template wording.
"""
from __future__ import annotations

import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

from blueprints import admin  # noqa: E402

DETAIL = (REPO / 'report-portal' / 'templates' /
          'admin_atm_detail.html').read_text()


def test_item_delay_and_age_labels():
    assert admin._item_delay_seconds('30s') == 30
    assert admin._item_delay_seconds('1m') == 60
    assert admin._item_delay_seconds('5m') == 300
    assert admin._item_delay_seconds('1h') == 3600
    assert admin._item_delay_seconds('1m/1m') == 60
    assert admin._item_delay_seconds('0') == 1
    assert admin._item_delay_seconds('nonsense') == 60
    assert admin._age_label(12) == '12 s ago'
    assert admin._age_label(120) == '2 min ago'
    assert admin._age_label(7200) == '2 h ago'


def _zabbix(monkeypatch, items, hosts=None):
    monkeypatch.setattr(admin, '_zbx_login', lambda: 'token')

    def call(method, _params, _auth, _retried=False):
        if method == 'host.get':
            return [{'hostid': '1'}] if hosts is None else hosts
        if method == 'item.get':
            return items
        raise AssertionError(method)

    monkeypatch.setattr(admin, '_zbx_call', call)


def _item(clock, delay='1m', kind='0'):
    return {'key_': 'atm.test', 'lastclock': str(clock),
            'delay': delay, 'type': kind}


def test_a_recent_independent_reading_is_reachable(monkeypatch):
    now = int(time.time())
    _zabbix(monkeypatch, [_item(now - 20)])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'reachable'
    assert status['age_seconds'] >= 20
    assert 'within the expected interval' in status['reason']


def test_an_old_reading_is_unreachable(monkeypatch):
    old = int(time.time()) - 6 * 60 * 60
    _zabbix(monkeypatch, [_item(old)])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'unreachable'
    assert status['last_clock'] == old
    assert status['age_label'].endswith('h ago')
    assert 'has not received' in status['reason']


def test_a_five_minute_item_is_not_called_dead_after_one_poll(monkeypatch):
    now = int(time.time())
    _zabbix(monkeypatch, [_item(now - 6 * 60, delay='5m')])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'reachable', (
        'a 5-minute WMI item is still fresh at six minutes')


def test_dependent_items_do_not_guess_their_master_cadence(monkeypatch):
    now = int(time.time())
    _zabbix(monkeypatch, [_item(now - 5, delay='0', kind='18')])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'unknown'
    assert 'no collected agent reading' in status['reason']


def test_fresh_snmp_or_http_items_do_not_prove_the_agent_is_alive(monkeypatch):
    now = int(time.time())
    _zabbix(monkeypatch, [
        _item(now - 10, delay='10s', kind='20'),   # SNMP agent, fresh
        _item(now - 10, delay='10s', kind='19'),   # HTTP agent, fresh
        _item(now - 6 * 60 * 60, delay='1m', kind='0'),  # real agent, dead
    ])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'unreachable', (
        'SNMP/HTTP polling must not be mistaken for TCP/10050 health')


def test_only_non_agent_items_is_unknown(monkeypatch):
    now = int(time.time())
    _zabbix(monkeypatch, [_item(now - 10, delay='10s', kind='20')])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'unknown'


def test_a_missing_host_is_unknown_not_unreachable(monkeypatch):
    _zabbix(monkeypatch, [], hosts=[])
    status = admin._fetch_agent_status('ATM-TEST-GRG')
    assert status['state'] == 'unknown'
    assert 'not enrolled' in status['reason']


def test_a_zabbix_read_failure_is_none(monkeypatch):
    monkeypatch.setattr(admin, '_zbx_login', lambda: None)
    assert admin._fetch_agent_status('ATM-TEST-GRG') is None


def test_detail_page_separates_agent_from_the_device_network_chip():
    assert 'agent_status' in DETAIL
    assert 'Zabbix agent reachable' in DETAIL
    assert 'Zabbix agent unreachable' in DETAIL
    assert 'device-reported Network chip' in DETAIL
    assert 'hw-qual' in DETAIL
    assert "h.name == 'Network'" in DETAIL


def test_detail_route_fetches_agent_status():
    source = (REPO / 'report-portal' / 'blueprints' / 'admin.py').read_text()
    assert 'fetch_agent_status(atm_id)' in source
    assert 'agent_status=agent_status' in source
