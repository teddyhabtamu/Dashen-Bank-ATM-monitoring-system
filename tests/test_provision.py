"""Tests for report-portal/provision.py — the provisioning front door.

The important guarantees: the database row is what gets provisioned (not a
caller's dict), unknown vendors are stopped for review rather than attached to
a template, and dry run is the default so a caller must opt in to writing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

import provision  # noqa: E402
import provision_state  # noqa: E402


@pytest.fixture(autouse=True)
def no_state_database(monkeypatch):
    """The provisioner's tests are about planning, not PostgreSQL.

    provision_atms() now records every write in the durable state table. The
    unit tests monkeypatch the Zabbix side, so the state write must be stubbed
    too; otherwise each test would try to open a real DB connection.
    """
    monkeypatch.setattr(provision_state, 'record_outcomes', lambda results: None)


ROWS = [
    {'atm_id': 'ATM-0001', 'vendor': 'NCR', 'model': 'SelfServ 34', 'district': 'NAD',
     'ip_address': '10.0.0.1', 'status': 'active', 'atm_name': 'One'},
    {'atm_id': 'ATM-0002', 'vendor': 'GRG', 'model': 'AD2600', 'district': 'HAWASA',
     'ip_address': '10.0.0.2', 'status': 'active', 'atm_name': 'Two'},
    {'atm_id': 'ATM-0003', 'vendor': 'MYSTERY', 'model': '??', 'district': 'NAD',
     'ip_address': '10.0.0.3', 'status': 'active', 'atm_name': 'Three'},
]


def install(monkeypatch, rows, **sync_kwargs):
    def fake_load(ids=None, active_only=False):
        if ids:
            keep = {i for i in ids}
            return [r for r in rows if r['atm_id'] in keep]
        return rows
    monkeypatch.setattr(provision, 'load_atms', fake_load)
    calls = []

    def fake_plan(atms):
        calls.append(('plan', [a['atm_id'] for a in atms]))
        return ({'created': len(atms), 'reenabled': 0, 'macros_fixed': 0, 'updated': 0,
                 'skipped': 0, 'no_ip': 0, 'failed': 0, 'details': []},
                [{'atm_id': a['atm_id'], 'action': 'create', 'changes': ['host'],
                  'pending_groups': [], 'error': ''} for a in atms])

    def fake_sync(atms):
        calls.append(('sync', [a['atm_id'] for a in atms]))
        return ({'created': len(atms), 'reenabled': 0, 'macros_fixed': 0, 'updated': 0,
                 'skipped': 0, 'no_ip': 0, 'failed': 0, 'details': []},
                [{'atm_id': a['atm_id'], 'action': 'create', 'changes': ['host'],
                  'pending_groups': [], 'applied': True, 'error': ''} for a in atms])

    monkeypatch.setattr(provision, 'plan_hosts', fake_plan)
    monkeypatch.setattr(provision, 'sync_hosts_detailed', fake_sync)
    return calls


def test_unknown_vendor_is_never_sent_to_zabbix(monkeypatch):
    calls = install(monkeypatch, ROWS)
    out = provision.provision_atms(None, dry_run=True)
    sent = calls[0][1]
    assert sent == ['ATM-0001', 'ATM-0002'], 'unknown-vendor ATM must not reach the API'
    assert [r['reason'] for r in out['review']] == \
        ['unknown vendor "MYSTERY" — needs engineer review']
    assert any(r['action'] == 'review' for r in out['results'])


def test_dry_run_is_the_default(monkeypatch):
    calls = install(monkeypatch, ROWS)
    out = provision.provision_atms(None)
    assert out['dry_run'] is True
    assert calls[0][0] == 'plan', 'default must not write'


def test_explicit_write_uses_the_apply_path(monkeypatch):
    calls = install(monkeypatch, ROWS)
    out = provision.provision_atms(['ATM-0001'], dry_run=False, actor='admin')
    assert calls[0][0] == 'sync'
    assert out['dry_run'] is False and out['actor'] == 'admin'
    assert out['results'][0]['action'] == 'create'


def test_results_cover_every_requested_atm(monkeypatch):
    install(monkeypatch, ROWS)
    out = provision.provision_atms(None, dry_run=True)
    got = sorted(r['atm_id'] for r in out['results'])
    assert got == ['ATM-0001', 'ATM-0002', 'ATM-0003']


def test_failed_write_is_reported_as_failed_not_created(monkeypatch):
    install(monkeypatch, ROWS[:1])

    def fake_sync(atms):
        return ({'created': 0, 'reenabled': 0, 'macros_fixed': 0, 'updated': 0,
                 'skipped': 0, 'no_ip': 0, 'failed': 1,
                 'details': [{'atm_id': 'ATM-0001', 'error': 'host.create failed'}]},
                [{'atm_id': 'ATM-0001', 'action': 'create', 'changes': ['host'],
                  'pending_groups': [], 'applied': False, 'error': 'host.create failed'}])

    import provision as p
    p.sync_hosts_detailed = fake_sync
    out = p.provision_atms(['ATM-0001'], dry_run=False)
    assert out['results'][0]['action'] == 'failed'
    assert out['ok'] is False


def test_db_failure_is_reported_not_raised(monkeypatch):
    def boom(ids=None, active_only=False):
        raise RuntimeError('connection refused')
    monkeypatch.setattr(provision, 'load_atms', boom)
    out = provision.provision_atms(['ATM-0001'], dry_run=True)
    assert out['ok'] is False and 'connection refused' in out['error']
    assert out['results'] == []


def test_no_records_is_not_a_failure(monkeypatch):
    install(monkeypatch, [])
    out = provision.provision_atms(['ATM-NOPE'], dry_run=True)
    assert out['ok'] is True and 'No matching' in out['error']


def test_a_narrower_row_set_can_never_widen_the_write(monkeypatch):
    """Belt and braces: even if load_atms ever returns extra rows, the
    provisioner must only ever touch the ATMs it was asked about."""
    monkeypatch.setattr(provision, 'load_atms', lambda ids=None, active_only=False: ROWS)
    calls = []
    monkeypatch.setattr(provision, 'sync_hosts_detailed', lambda a: (
        {'created': 0, 'reenabled': 0, 'macros_fixed': 0, 'updated': 0, 'skipped': 0,
         'no_ip': 0, 'failed': 0, 'details': []},
        [{'atm_id': r['atm_id'], 'action': 'skipped', 'changes': [], 'applied': False,
          'error': ''} for r in a]))
    provision.provision_atms(['ATM-0002'], dry_run=False)
    assert [r['atm_id'] for r in provision.provision_atms(['ATM-0002'], dry_run=False)['results']] == ['ATM-0002']
    assert calls == []


def test_provision_one_targets_a_single_atm(monkeypatch):
    calls = install(monkeypatch, ROWS)
    provision.provision_one('ATM-0002', dry_run=True, actor='wizard')
    assert calls[0][1] == ['ATM-0002']


def test_reindex_covers_every_active_atm(monkeypatch):
    seen = {}

    def fake_load(ids=None, active_only=False):
        seen['active_only'] = active_only
        return ROWS[:2]
    monkeypatch.setattr(provision, 'load_atms', fake_load)
    monkeypatch.setattr(provision, 'plan_hosts',
                        lambda a: ({'created': 0, 'reenabled': 0, 'macros_fixed': 0,
                                    'updated': 0, 'skipped': 2, 'no_ip': 0, 'failed': 0,
                                    'details': []}, []))
    out = provision.reindex_active(dry_run=True)
    assert seen['active_only'] is True
    assert out['summary']['skipped'] == 2


def test_review_queue_reports_only_unknown_vendors():
    assert [r['atm_id'] for r in provision.review_queue(ROWS)] == ['ATM-0003']
    assert provision.review_queue([]) == []


def test_a_write_records_the_durable_state(monkeypatch):
    install(monkeypatch, ROWS)
    recorded = []
    monkeypatch.setattr(provision_state, 'record_outcomes',
                        lambda results: recorded.extend(results))
    provision.provision_atms(['ATM-0001'], dry_run=False, actor='test')
    assert [r['atm_id'] for r in recorded] == ['ATM-0001']
    assert recorded[0]['action'] == 'create'


def test_a_dry_run_never_records_state(monkeypatch):
    install(monkeypatch, ROWS)
    recorded = []
    monkeypatch.setattr(provision_state, 'record_outcomes',
                        lambda results: recorded.extend(results))
    provision.provision_atms(['ATM-0001'], dry_run=True, actor='test')
    assert recorded == []