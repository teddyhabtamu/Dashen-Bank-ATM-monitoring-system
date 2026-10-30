"""Tests for report-portal/verify.py — the "did it actually report?" gate.

This is the gate that stops a 1,202-ATM rollout from looking green on paper
while machines stay dark, so the cases that matter are the in-between ones:
host exists, items exist, and yet nothing is arriving.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import verify  # noqa: E402

NOW = int(time.time())


class FakeZabbix:
    def __init__(self, hosts=None, items=None, triggers=None):
        self.hosts = hosts if hosts is not None else []
        self.items = items or []
        self.triggers = triggers or []
        self.last_error = ''

    def login(self):
        return True

    def call(self, method, params):
        if method == 'host.get':
            if 'filter' in params:
                return [h for h in self.hosts if h['host'] in params['filter']['host']]
            if 'hostids' in params:
                return [h for h in self.hosts if h['hostid'] in params['hostids']]
            return self.hosts
        if method == 'item.get':
            want = params.get('hostids') or []
            # items without an explicit hostid answer for whichever host asked
            return [i for i in self.items
                    if not want or i.get('hostid') is None or i['hostid'] in want]
        if method == 'trigger.get':
            return self.triggers
        return None


def host(name='ATM-0001', hostid='1', status='0', interfaces=None):
    return {'hostid': hostid, 'host': name, 'status': status,
            'interfaces': interfaces if interfaces is not None else [
                {'type': '2', 'port': '161', 'ip': '10.0.0.1'},
                {'type': '1', 'port': '10050', 'ip': '10.0.0.1'}]}


def item(lastclock, state='0', key='atm.net.link', hostid=None, type='2'):
    # type '2' = Zabbix agent passive, type '0' = also passive/UserParameter,
    # type '17' = dependent item (JSONPath parts derived from atm.parts).
    # Internal items (zabbix[...]) are ALSO type '0' - so internal items must
    # be recognised by their key, never by their type.
    return {'itemid': 'i', 'key_': key, 'lastclock': str(lastclock),
            'state': state, 'hostid': hostid, 'type': type}


def ncr_item(age_seconds=60, **kw):
    return item(int(time.time()) - age_seconds, key='atm.parts', **kw)


def grg_item(age_seconds=60, **kw):
    return item(int(time.time()) - age_seconds, key='atm.net.link', **kw)


def grg_core(age_seconds=60, **kw):
    return [item(int(time.time()) - age_seconds, key=k, **kw)
            for k in ('atm.status', 'atm.net.link', 'atm.cash.cassette1')]


def grg_xfs(age_seconds=60, **kw):
    return [item(int(time.time()) - age_seconds, key=k, **kw)
            for k in ('atm.xfs.cdm.device_status', 'atm.xfs.cdm.cassette.count[0050A]')]


def internal_item(hostid=None):
    return item(int(time.time()), key='zabbix[host,agent,available]', hostid=hostid, type='0')


def ncr_master(age_seconds=60, hostid=None):
    """The real shape of the WMI master item: a UserParameter, so Zabbix
    reports type '0' - the same type internal items use."""
    return item(int(time.time()) - age_seconds, key='atm.parts', hostid=hostid, type='0')


def dependent_part(age_seconds=60, hostid=None, name='Card Reader'):
    return item(int(time.time()) - age_seconds, key=f'atm.part.part_status[{name}]',
                hostid=hostid, type='17')


def db(vendor='NCR', ej_hostname=None, state='UNKNOWN', ip='10.0.0.1'):
    return {'row': {'atm_id': 'ATM-0001', 'vendor': vendor, 'model': 'SelfServ 34',
                    'status': 'active', 'ip_address': ip, 'ej_hostname': ej_hostname},
            'state': state, 'last_seen': '2026-10-15 08:40', 'db_error': ''}


# ── the happy path ───────────────────────────────────────────────────────
def test_fresh_items_mean_reporting():
    z = FakeZabbix([host()], items=[ncr_master(60), item(NOW - 30, key='atm.net.link')])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'reporting' and r['ok'] is True
    assert r['facts']['items_reporting'] == 2
    assert any(c['name'] == 'items' and c['status'] == 'pass' for c in r['checks'])


def test_boundary_just_inside_the_window_is_ok():
    z = FakeZabbix([host()], items=[ncr_master(14 * 60)])
    assert verify.verify_atm('ATM-0001', client=z, db_facts=db())['verdict'] == 'reporting'


# ── the in-between cases that matter ─────────────────────────────────────
def test_host_exists_but_nothing_ever_collected():
    z = FakeZabbix([host()], items=[item(0), item(0)])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting'
    assert r['ok'] is False
    assert any('ever collected' in b for b in r['blockers'])


def test_all_items_unsupported_names_the_agent():
    z = FakeZabbix([host()], items=[item(0, state='1'), item(0, state='1')])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting'
    assert any('UNSUPPORTED' in b and 'agent' in b for b in r['blockers'])


def test_reporting_but_stale_is_a_warning_not_a_pass():
    z = FakeZabbix([host()], items=[ncr_master(3 * 3600)])
    r = verify.verify_atm('ATM-0001', window_minutes=15, client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting'
    warn = [c for c in r['checks'] if c['name'] == 'items'][0]
    assert warn['status'] == 'warn' and '180 min' in warn['detail']


def test_host_with_no_items_is_a_configuration_blocker():
    z = FakeZabbix([host()], items=[])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting'
    assert any('template is not linked' in b for b in r['blockers'])


def test_missing_host_is_not_provisioned():
    z = FakeZabbix([], items=[item(NOW)])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'not_provisioned'
    assert any('Not provisioned' in b for b in r['blockers'])


def test_disabled_host_blocks_even_with_fresh_data():
    z = FakeZabbix([host(status='1')], items=[ncr_master(60)])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['ok'] is False
    assert any('disabled' in b for b in r['blockers'])


# ── NCR needs the agent interface; GRG does not ──────────────────────────
def test_ncr_without_agent_interface_is_blocked():
    snmp_only = [{'type': '2', 'port': '161', 'ip': '10.0.0.1'}]
    z = FakeZabbix([host(interfaces=snmp_only)], items=[ncr_master(60)])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db(vendor='NCR'))
    assert r['ok'] is False
    assert any('agent interface' in b for b in r['blockers'])


def test_grg_legacy_snmp_is_still_accepted():
    snmp_only = [{'type': '2', 'port': '161', 'ip': '10.0.0.1'}]
    z = FakeZabbix([host(interfaces=snmp_only)], items=grg_core(60))
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db(vendor='GRG'))
    assert r['verdict'] == 'reporting' and r['ok'] is True


def test_grg_xfs_is_reporting():
    snmp_only = [{'type': '2', 'port': '161', 'ip': '10.0.0.1'}]
    z = FakeZabbix([host(interfaces=snmp_only)], items=grg_xfs(60))
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db(vendor='GRG'))
    assert r['verdict'] == 'reporting' and r['ok'] is True
    assert any('atm.xfs.cdm' in c['detail'] for c in r['checks'] if c['name'] == 'telemetry')


def test_grg_stale_xfs_is_not_hidden_by_fresh_snmp():
    """The UAT contradiction: Fleet health said Reporting because the SNMP core
    was fresh, while the ATM page correctly said the Zabbix agent was
    unreachable. A source that was collected before and has gone quiet is a
    partial telemetry outage, not a healthy row."""
    snmp_only = [{'type': '2', 'port': '161', 'ip': '10.0.0.1'}]
    z = FakeZabbix([host(interfaces=snmp_only)],
                   items=grg_core(30) + grg_xfs(25 * 60))
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db(vendor='GRG'))
    assert r['verdict'] == 'provisioned_not_reporting'
    assert any('XFS' in b and 'agent' in b for b in r['blockers'])
    assert any(c['name'] == 'secondary telemetry' and 'XFS' in c['detail']
               for c in r['checks'])


def test_a_userparameter_master_item_is_not_mistaken_for_internal():
    """Regression #2, found on UAT: atm.parts is a UserParameter and Zabbix
    reports it as type '0' - the same type as internal items. Classifying by
    type threw away the one key the gate requires, and a live ATM came back
    SILENT."""
    z = FakeZabbix([host()], items=[ncr_master(60, hostid=None),
                                    dependent_part(60),
                                    internal_item()])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'reporting', r['verdict']
    assert r['facts']['items_internal'] == 1
    assert r['facts']['items_reporting'] == 2, 'master + dependent both count'


def test_internal_items_alone_are_never_reporting():
    """Regression from UAT: a dead ATM passed because one Zabbix-internal
    item always ticks. `1/90 items reporting` said nothing about the ATM."""
    z = FakeZabbix([host()], items=[internal_item()])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting' and r['ok'] is False
    assert r['facts']['items_internal'] == 1 and r['facts']['items_reporting'] == 0
    assert any(c['name'] == 'internal items' for c in r['checks'])
    assert any('atm.parts' in b for b in r['blockers'])


def test_many_healthy_items_without_vendor_telemetry_is_still_silent():
    """`1/90 reporting` on a dead host: many items exist, none is the vendor's
    own signal, so the ATM is not reporting."""
    items = [item(0, key=f'atm.parts[{i}]') for i in range(89)] + [internal_item()]
    z = FakeZabbix([host()], items=items)
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    assert r['verdict'] == 'provisioned_not_reporting'
    assert any('atm.parts' in b for b in r['blockers'])


def test_grg_needs_its_own_core_keys():
    z = FakeZabbix([host()], items=[grg_item(60)])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db(vendor='GRG'))
    assert r['verdict'] == 'provisioned_not_reporting'
    assert any('No GRG telemetry' in b for b in r['blockers'])


def test_ncr_grg_silence_names_the_right_cause():
    ncr = verify.verify_atm('ATM-0001', client=FakeZabbix([host()], items=[]),
                            db_facts=db(vendor='NCR'))
    grg = verify.verify_atm('ATM-0001', client=FakeZabbix([host()], items=[]),
                            db_facts=db(vendor='GRG'))
    ncr_detail = [c['detail'] for c in ncr['checks'] if c['name'] == 'telemetry'][0]
    grg_detail = [c['detail'] for c in grg['checks'] if c['name'] == 'telemetry'][0]
    assert '10050' in ncr_detail and 'atm-wmi-collector' in ncr_detail
    assert 'XFS' in grg_detail


# ── evidence gaps must not be mistaken for good news ─────────────────────
def test_missing_atm_record_is_reported_not_assumed():
    z = FakeZabbix([host()], items=[ncr_master(60)])
    empty = {'row': {}, 'state': '', 'last_seen': '', 'db_error': ''}
    r = verify.verify_atm('ATM-0001', client=z, db_facts=empty)
    assert any(c['name'] == 'portal record' and c['status'] == 'fail' for c in r['checks'])
    assert any('No ATM record' in b for b in r['blockers'])


def test_zabbix_outage_is_unknown_not_healthy():
    class Dead:
        def login(self):
            return False

        def call(self, *a, **k):
            self.last_error = 'connection refused'
            return None
    r = verify.verify_atm('ATM-0001', client=Dead(), db_facts=db())
    assert r['verdict'] == 'unknown' and r['ok'] is False
    assert any('unverified' in b for b in r['blockers'])


def test_journal_check_is_informational_only():
    z = FakeZabbix([host()], items=[ncr_master(60)])
    with_ej = verify.verify_atm('ATM-0001', client=z, db_facts=db(ej_hostname='TESTMACHINE'))
    without = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    j1 = [c for c in with_ej['checks'] if c['name'] == 'journal'][0]
    j2 = [c for c in without['checks'] if c['name'] == 'journal'][0]
    assert j1['status'] == 'info' and j2['status'] == 'skip'
    assert with_ej['verdict'] == without['verdict'] == 'reporting'


def test_firing_triggers_are_surfaced():
    z = FakeZabbix([host()], items=[ncr_master(60)],
                   triggers=[{'triggerid': 't', 'description': 'ATM is unavailable', 'priority': '4'}])
    r = verify.verify_atm('ATM-0001', client=z, db_facts=db())
    prob = [c for c in r['checks'] if c['name'] == 'problems'][0]
    assert 'ATM is unavailable' in prob['detail']


def test_verify_all_counts_verdicts():
    class Multi(FakeZabbix):
        def __init__(self):
            super().__init__(hosts=[host(name='GOOD', hostid='1'),
                                    host(name='SILENT', hostid='2')],
                             items=[ncr_master(60, hostid='1'),
                                      item(0, key='atm.parts', hostid='2')])

        def call(self, method, params):
            if method == 'host.get' and 'filter' in params:
                name = params['filter']['host'][0]
                return [h for h in self.hosts if h['host'] == name]
            return super().call(method, params)
    z = Multi()
    out = verify.verify_all(['GOOD', 'SILENT', 'MISSING'], client=z)
    assert out['checked'] == 3
    verdicts = {r['atm_id']: r['verdict'] for r in out['results']}
    assert verdicts['GOOD'] == 'reporting'
    assert verdicts['SILENT'] == 'provisioned_not_reporting'
    assert verdicts['MISSING'] == 'not_provisioned'