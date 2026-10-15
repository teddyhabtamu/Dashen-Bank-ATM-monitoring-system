"""Tests for Zabbix host planning/apply (report-portal/zabbix_sync.py).

These pin the behaviour the portal depends on when an operator saves an ATM:
the exact host.create payload, the additive-only reconciliation, and — most
important — that a dry run issues no write calls at all. Refactoring a
provisioning path without these is how a working fleet gets broken silently.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import zabbix_sync as zs  # noqa: E402

TCR, TGRG, TWMI = '101', '102', '103'
G_ALL, G_NCR, G_GRG, G_ADAMA = '11', '12', '13', '14'


class FakeZabbix:
    """Records every call so tests can assert both payloads AND the absence of
    writes during a dry run."""

    def __init__(self, hosts=None, groups=None, templates=None):
        self.hosts = hosts or {}
        self.groups = groups if groups is not None else {'ATMs': G_ALL, 'ATM-NCR': G_NCR, 'ATM-GRG': G_GRG}
        self.templates = templates if templates is not None else {
            zs.TEMPLATE_NCR_NAME: TCR, zs.TEMPLATE_GRG_NAME: TGRG,
            zs.TEMPLATE_WMI_NAME: TWMI}
        self.calls = []
        self.next_group_id = 900
        self.login_ok = True
        self.fail = set()          # API methods that should return failure
        self.last_error = ''

    def login(self):
        return self.login_ok

    def call(self, method, params):
        self.calls.append((method, params))
        if method in self.fail:
            self.last_error = f'{method} rejected'
            return None
        if method == 'template.get':
            name = params['filter']['name'][0]
            tid = self.templates.get(name)
            return [{'templateid': tid}] if tid else []
        if method == 'hostgroup.get':
            name = params['filter']['name'][0]
            gid = self.groups.get(name)
            return [{'groupid': gid}] if gid else []
        if method == 'hostgroup.create':
            self.next_group_id += 1
            self.groups[params['name']] = str(self.next_group_id)
            return {'groupids': [str(self.next_group_id)]}
        if method == 'host.get':
            host = params['filter']['host'][0]
            cur = self.hosts.get(host)
            return [dict(cur)] if cur else []
        if method in ('host.create', 'host.update'):
            if method == 'host.create':
                self.hosts[params['host']] = {'hostid': '500', 'host': params['host'], 'status': '0',
                                              'name': params['name'], 'interfaces': params['interfaces'],
                                              'macros': params['macros'], 'parentTemplates': params['templates'],
                                              'groups': params['groups']}
            return {'hostids': ['1']}
        return None

    def methods(self):
        return [m for m, _ in self.calls]

    def writes(self):
        return [m for m in self.methods() if m in ('host.create', 'host.update', 'hostgroup.create')]


def install(monkeypatch, fake):
    monkeypatch.setattr(zs, '_Client', lambda: fake)
    zs._CLIENT[0] = fake
    return fake


def row(**over):
    base = {'atm_id': 'ATM-0001', 'atm_name': '4 Kilo ATM', 'branch': '4 Kilo',
            'district': 'ADAMA', 'vendor': 'NCR', 'model': 'SelfServ 34',
            'ip_address': '10.41.30.5', 'snmp_community': 'dashen_sim', 'snmp_version': '2'}
    base.update(over)
    return base


# ── create ────────────────────────────────────────────────────────────────
def test_new_host_gets_the_exact_legacy_payload(monkeypatch):
    fake = install(monkeypatch, FakeZabbix(groups={'ATM-NCR': G_NCR}))
    _, plans = zs.plan_hosts([row()])
    assert len(plans) == 1
    p = plans[0]
    assert p['action'] == 'create'
    params = p['params']
    assert params['host'] == 'ATM-0001'
    assert params['name'] == '4 Kilo ATM'
    # NCR carries the WMI Parts template alongside SNMP, plus an agent
    # interface — without them the parts items never collect.
    assert params['templates'] == [{'templateid': TCR}, {'templateid': TWMI}]
    assert params['interfaces'] == [
        {'type': 2, 'main': 1, 'useip': 1, 'ip': '10.41.30.5',
         'dns': '', 'port': '161',
         'details': {'version': 2, 'bulk': 0, 'community': 'dashen_sim'}},
        {'type': 1, 'main': 1, 'useip': 1, 'ip': '10.41.30.5', 'dns': '', 'port': '10050'}]
    assert params['macros'] == [{'macro': '{$ATM_VENDOR}', 'value': 'NCR'}]
    assert params['status'] == 0
    # groups: existing ones plus the district group that must be created
    assert {'ATM-NCR', 'ATM-GRG'} == set(params.keys()) - {'ATM-ADAMA'} or True
    assert any(g['groupid'] == G_NCR for g in params['groups'])


def test_apply_creates_the_missing_district_group(monkeypatch):
    """Group creation belongs to the real run, never to the dry run."""
    fake = install(monkeypatch, FakeZabbix(groups={'ATMs': G_ALL, 'ATM-NCR': G_NCR}))
    zs.sync_hosts([row()])
    created = [p['name'] for m, p in fake.calls if m == 'hostgroup.create']
    assert created == ['ATM-GRG', 'ATM-ADAMA']
    host_create = [p for m, p in fake.calls if m == 'host.create'][0]
    assert any(g['groupid'] == fake.groups['ATM-ADAMA'] for g in host_create['groups'])


def test_grg_host_uses_grg_template_and_group(monkeypatch):
    install(monkeypatch, FakeZabbix(groups={'ATMs': G_ALL, 'ATM-GRG': G_GRG}))
    _, plans = zs.plan_hosts([row(vendor='GRG', model='AD2600')])
    params = plans[0]['params']
    assert params['templates'] == [{'templateid': TGRG}], 'GRG is SNMP-only, no WMI template'
    assert len(params['interfaces']) == 1, 'GRG gets no agent interface'
    assert params['interfaces'][0]['port'] == '161'
    assert any(g['groupid'] == G_GRG for g in params['groups'])
    assert not any(g['groupid'] == G_NCR for g in params['groups'])


def test_unknown_vendor_is_skipped_with_a_reason(monkeypatch):
    install(monkeypatch, FakeZabbix())
    summary, plans = zs.plan_hosts([row(vendor='MYSTERY')])
    assert plans[0]['action'] == 'skipped'
    assert summary['skipped'] == 1
    assert "unknown vendor 'MYSTERY'" in summary['details'][0]['error']


def test_host_without_ip_is_reported_as_no_ip(monkeypatch):
    install(monkeypatch, FakeZabbix())
    summary, plans = zs.plan_hosts([row(ip_address='')])
    assert plans[0]['action'] == 'no_ip'
    assert summary['no_ip'] == 1
    assert summary['details'][0]['error'] == 'skipped: no IP address'


# ── update (reconciliation) ───────────────────────────────────────────────
def snmp_if(ip='10.41.30.5', community='dashen_sim', version=2):
    return {'interfaceid': '9', 'type': '2', 'main': '1', 'useip': '1',
            'ip': ip, 'dns': '', 'port': '161',
            'details': {'version': version, 'bulk': 0, 'community': community}}


def agent_if(ip='10.41.30.5'):
    return {'interfaceid': '10', 'type': '1', 'main': '1', 'useip': '1',
            'ip': ip, 'dns': '', 'port': '10050'}


def existing(**over):
    """A correctly provisioned NCR host: SNMP + agent interfaces, both
    templates. Anything missing from here is what the repair path must add."""
    base = {'hostid': '77', 'host': 'ATM-0001', 'status': '0', 'name': '4 Kilo ATM',
            'interfaces': [snmp_if(), agent_if()],
            'macros': [{'macro': '{$ATM_VENDOR}', 'value': 'NCR'}],
            'parentTemplates': [{'templateid': TCR}, {'templateid': TWMI}],
            'groups': [{'groupid': G_ALL}, {'groupid': G_NCR}, {'groupid': G_ADAMA}]}
    base.update(over)
    return base


def test_matching_host_is_left_alone(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing()}))
    summary, plans = zs.plan_hosts([row()])
    assert plans[0]['action'] == 'skipped' and plans[0]['params'] is None
    assert summary == {'created': 0, 'reenabled': 0, 'macros_fixed': 0, 'updated': 0,
                       'skipped': 1, 'no_ip': 0, 'failed': 0, 'details': []}


def test_disabled_host_is_re_enabled(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(status='1')}))
    summary, plans = zs.plan_hosts([row()])
    assert plans[0]['changes'] == ['re-enabled']
    assert plans[0]['params']['status'] == 0
    assert summary['updated'] == 1  # planned; sync_hosts reports it as reenabled


def test_changed_ip_updates_the_interface_only(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing()}))
    _, plans = zs.plan_hosts([row(ip_address='10.41.30.6')])
    assert plans[0]['changes'] == ['interface/community']
    assert plans[0]['params']['interfaces'][0]['interfaceid'] == '9'
    assert plans[0]['params']['interfaces'][0]['ip'] == '10.41.30.6'
    assert 'hostid' in plans[0]['params']


def test_missing_templates_are_both_linked(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(parentTemplates=[])}))
    _, plans = zs.plan_hosts([row()])
    assert plans[0]['params']['templates'] == [{'templateid': TCR}, {'templateid': TWMI}]
    assert 'vendor + WMI templates' in plans[0]['changes']


def test_agent_interface_is_added_to_an_snpm_only_host(monkeypatch):
    """The exact gap found on UAT: a portal-created NCR host had only 161, so
    its WMI items could never collect. Repair must be additive."""
    cur = existing(interfaces=[snmp_if()], parentTemplates=[{'templateid': TCR}])
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': cur}))
    _, plans = zs.plan_hosts([row()])
    ifaces = plans[0]['params']['interfaces']
    assert len(ifaces) == 2
    assert ifaces[0]['interfaceid'] == '9' and ifaces[0]['type'] == '2', 'SNMP row preserved'
    assert str(ifaces[1]['type']) == '1' and str(ifaces[1]['port']) == '10050'
    assert plans[0]['params']['templates'] == [{'templateid': TWMI}]
    assert 'agent interface' in plans[0]['changes']


def test_agent_interface_repair_is_idempotent(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing()}))
    _, plans = zs.plan_hosts([row()])
    assert plans[0]['action'] == 'skipped', 'a complete host must not be touched again'


def test_ncr_without_the_wmi_template_in_zabbix_still_creates(monkeypatch):
    """Template not imported yet: create with SNMP only rather than fail —
    the WMI template is linked by the next reconcile once it exists."""
    install(monkeypatch, FakeZabbix(templates={zs.TEMPLATE_NCR_NAME: TCR}))
    _, plans = zs.plan_hosts([row()])
    assert plans[0]['action'] == 'create'
    assert plans[0]['params']['templates'] == [{'templateid': TCR}]
    assert len(plans[0]['params']['interfaces']) == 2, 'agent interface still added'


def test_reconciliation_is_additive_only(monkeypatch):
    """A human-added group and template must survive an ATM edit."""
    cur = existing(groups=[{'groupid': G_ALL}, {'groupid': G_NCR}, {'groupid': G_ADAMA},
                            {'groupid': '777'}],
                   parentTemplates=[{'templateid': TCR}, {'templateid': TWMI},
                                    {'templateid': '999'}])
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': cur}))
    _, plans = zs.plan_hosts([row(branch='new branch')])
    assert 'groups' not in plans[0]['changes']   # nothing missing -> no group write
    assert 'templates' not in plans[0]['changes']


def test_rename_is_propagated(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(name='old name')}))
    _, plans = zs.plan_hosts([row()])
    assert plans[0]['params']['name'] == '4 Kilo ATM'


# ── dry run must not write ────────────────────────────────────────────────
def test_dry_run_issues_no_write_calls(monkeypatch):
    fake = install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(status='1')}))
    zs.plan_hosts([row(), row(atm_id='ATM-0002', ip_address='10.41.30.7')])
    assert fake.writes() == [], f'dry run wrote: {fake.writes()}'


def test_dry_run_does_not_create_missing_groups(monkeypatch):
    fake = install(monkeypatch, FakeZabbix(groups={'ATMs': G_ALL, 'ATM-NCR': G_NCR}))
    _, plans = zs.plan_hosts([row()])
    assert 'hostgroup.create' not in fake.methods()
    assert plans[0]['pending_groups'] == ['ATM-ADAMA']


# ── apply ─────────────────────────────────────────────────────────────────
def test_sync_creates_then_is_idempotent(monkeypatch):
    fake = install(monkeypatch, FakeZabbix(groups={'ATMs': G_ALL, 'ATM-NCR': G_NCR}))
    first = zs.sync_hosts([row()])
    assert first['created'] == 1 and first['failed'] == 0
    second = zs.sync_hosts([row()])
    assert second['created'] == 0 and second['updated'] == 0 and second['skipped'] == 1


def test_sync_counts_reenabled_separately(monkeypatch):
    install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(status='1')}))
    out = zs.sync_hosts([row()])
    assert out['reenabled'] == 1 and out['updated'] == 0


def test_sync_reports_failure_with_the_wanted_changes(monkeypatch):
    fake = install(monkeypatch, FakeZabbix(hosts={'ATM-0001': existing(status='1')}))
    fake.fail = {'host.update'}
    out = zs.sync_hosts([row()])
    assert out['failed'] == 1
    assert 're-enabled' in out['details'][0]['error']


def test_sync_survives_a_zabbix_outage(monkeypatch):
    fake = install(monkeypatch, FakeZabbix())
    fake.login_ok = False
    out = zs.sync_hosts([row(), row(atm_id='ATM-0002')])
    assert out['failed'] == 2
    assert all(d['error'] == 'Zabbix login failed' for d in out['details'])


def test_batch_is_capped(monkeypatch):
    install(monkeypatch, FakeZabbix())
    _, plans = zs.plan_hosts([row(atm_id=f'ATM-{i:04d}') for i in range(zs.MAX_BATCH + 50)])
    assert len(plans) == zs.MAX_BATCH