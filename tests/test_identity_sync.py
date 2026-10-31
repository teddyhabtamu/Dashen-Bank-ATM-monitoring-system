"""Automatic identity ingest: transport, validation, and the no-write cases.

The identity probe is already covered by test_agent_identity.py. These tests
cover the new link: reading a Zabbix item's lastvalue, refusing bad evidence,
and not rewriting an unchanged document.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import identity_sync  # noqa: E402
from atm_identity import parse  # noqa: E402


def identity():
    return {
        'schema': 1,
        'collected_at': '2026-10-30T09:00:00Z',
        'computer': 'ATM-PC-01',
        'vendor': 'NCR',
        'vendor_source': 'aptra',
        'model': 'SelfServ 82',
        'serial': 'SN-123',
        'terminal_id': 'TID006',
        'terminal_source': 'ej-header',
        'ej': {
            'hostname': 'ATM-TEST-NCR',
            'hostname_source': 'ej-filename',
            'roots': ['C:\\ATM\\EJ'],
            'newest_file': 'EJ0001.log',
            'format': 'ncr',
        },
        'images': {'roots': ['C:\\ATM\\images'], 'candidates': []},
        'ip_addresses': ['10.41.30.10'],
        'probes': {
            'ncr-xfs-probe.ps1': {'sha256': 'a' * 64, 'version': '2026-10-29.3'},
        },
        'agent': {'version': '6.4.0', 'config_hash': 'b' * 64},
    }


ATM_ROW = {
    'vendor': 'NCR', 'terminal_id': 'TID006', 'ej_hostname': 'ATM-TEST-NCR',
    'model': 'SelfServ 82', 'ip_address': '10.41.30.10',
}


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.last_error = ''
        self.token = 'token'

    def call(self, method, params):
        self.calls.append((method, params))
        return self.responses.get(method)


def test_a_valid_value_is_stored_with_its_source_and_audit(monkeypatch):
    saved, audits = [], []
    monkeypatch.setattr(identity_sync, 'load_identity', lambda atm_id: None)
    monkeypatch.setattr(identity_sync, 'save_identity',
                        lambda atm_id, ident, conflicts, received_by='':
                        saved.append((atm_id, ident, conflicts, received_by)))
    monkeypatch.setattr(identity_sync, '_atm_row', lambda atm_id: ATM_ROW)
    monkeypatch.setattr(identity_sync, 'log_action',
                        lambda action, detail=None: audits.append((action, detail)))

    out = identity_sync.ingest_value('ATM-1', json.dumps(identity()))

    assert out['status'] == 'stored'
    assert saved and saved[0][0] == 'ATM-1'
    assert saved[0][1]['vendor'] == 'NCR'
    assert saved[0][2] == []
    assert saved[0][3] == 'zabbix:auto'
    assert audits and audits[0][0] == 'AGENT_IDENTITY_INGEST'
    assert 'source=zabbix:auto' in audits[0][1]


def test_bad_evidence_is_rejected_without_writing(monkeypatch):
    saved = []
    monkeypatch.setattr(identity_sync, 'save_identity',
                        lambda *args, **kwargs: saved.append(args))

    out = identity_sync.ingest_value('ATM-1', '{"schema": 2}')

    assert out['status'] == 'rejected'
    assert 'schema' in out['error']
    assert saved == []


def test_an_empty_ej_hostname_is_seeded_from_the_filename(monkeypatch):
    seeded, audits = [], []
    monkeypatch.setattr(identity_sync, 'load_identity', lambda atm_id: None)
    monkeypatch.setattr(identity_sync, 'save_identity', lambda *a, **k: None)
    monkeypatch.setattr(identity_sync, '_atm_row',
                        lambda atm_id: dict(ATM_ROW, ej_hostname=None))
    monkeypatch.setattr(identity_sync, 'set_ej_hostname_if_empty',
                        lambda atm_id, hostname:
                        seeded.append((atm_id, hostname)) or True)
    monkeypatch.setattr(identity_sync, 'log_action',
                        lambda action, detail=None: audits.append((action, detail)))

    out = identity_sync.ingest_value('ATM-1', json.dumps(identity()))

    assert out['status'] == 'stored'
    assert seeded == [('ATM-1', 'ATM-TEST-NCR')]
    assert any(action == 'AGENT_EJ_HOSTNAME_SET' for action, _ in audits)


def test_an_existing_ej_hostname_is_never_overwritten(monkeypatch):
    seeded = []
    monkeypatch.setattr(identity_sync, 'load_identity', lambda atm_id: None)
    monkeypatch.setattr(identity_sync, 'save_identity', lambda *a, **k: None)
    monkeypatch.setattr(identity_sync, '_atm_row',
                        lambda atm_id: dict(ATM_ROW, ej_hostname='OLD-NAME'))
    monkeypatch.setattr(identity_sync, 'set_ej_hostname_if_empty',
                        lambda atm_id, hostname: seeded.append((atm_id, hostname)))
    monkeypatch.setattr(identity_sync, 'log_action', lambda *a, **k: None)

    identity_sync.ingest_value('ATM-1', json.dumps(identity()))

    assert seeded == []


def test_a_weak_ej_source_does_not_seed_the_registry(monkeypatch):
    payload = identity()
    payload['ej']['hostname_source'] = 'computer-name'
    seeded = []
    monkeypatch.setattr(identity_sync, 'load_identity', lambda atm_id: None)
    monkeypatch.setattr(identity_sync, 'save_identity', lambda *a, **k: None)
    monkeypatch.setattr(identity_sync, '_atm_row',
                        lambda atm_id: dict(ATM_ROW, ej_hostname=None))
    monkeypatch.setattr(identity_sync, 'set_ej_hostname_if_empty',
                        lambda atm_id, hostname: seeded.append((atm_id, hostname)))
    monkeypatch.setattr(identity_sync, 'log_action', lambda *a, **k: None)

    identity_sync.ingest_value('ATM-1', json.dumps(payload))

    assert seeded == []


def test_an_unchanged_document_is_not_rewritten(monkeypatch):
    saved = []
    monkeypatch.setattr(identity_sync, 'load_identity',
                        lambda atm_id: {'payload': parse(identity())})
    monkeypatch.setattr(identity_sync, 'save_identity',
                        lambda *args, **kwargs: saved.append(args))

    out = identity_sync.ingest_value('ATM-1', json.dumps(identity()))

    assert out['status'] == 'unchanged'
    assert saved == []


def test_sync_all_reads_one_lastvalue_per_host(monkeypatch):
    rows = [{'atm_id': 'ATM-1', 'ip_address': '10.41.30.10'}]
    hosts = [{'hostid': '1', 'host': 'ATM-1'}]
    items = [{
        'itemid': '9', 'hostid': '1', 'key_': 'atm.identity',
        'lastvalue': json.dumps(identity()),
        'lastclock': str(int(time.time())), 'state': '0', 'error': '',
    }]
    seen = []
    monkeypatch.setattr('provision.load_atms', lambda ids=None, active_only=False: rows)
    monkeypatch.setattr(identity_sync, '_client', lambda: object())
    monkeypatch.setattr(identity_sync, '_hosts_and_items',
                        lambda client, ids: (hosts, items))
    monkeypatch.setattr(identity_sync, 'ingest_value',
                        lambda atm_id, raw, clock=None, source='zabbix:auto':
                        seen.append((atm_id, source)) or {'status': 'stored'})

    out = identity_sync.sync_all()

    assert out['checked'] == 1
    assert out['stored'] == 1
    assert out['missing_item'] == 0
    assert seen == [('ATM-1', 'zabbix:auto')]


def test_sync_all_skips_an_unsupported_item(monkeypatch):
    rows = [{'atm_id': 'ATM-1', 'ip_address': '10.41.30.10'}]
    hosts = [{'hostid': '1', 'host': 'ATM-1'}]
    items = [{
        'itemid': '9', 'hostid': '1', 'key_': 'atm.identity',
        'lastvalue': '', 'lastclock': '0', 'state': '1',
        'error': 'Unsupported item key.',
    }]
    monkeypatch.setattr('provision.load_atms', lambda ids=None, active_only=False: rows)
    monkeypatch.setattr(identity_sync, '_client', lambda: object())
    monkeypatch.setattr(identity_sync, '_hosts_and_items',
                        lambda client, ids: (hosts, items))

    out = identity_sync.sync_all()

    assert out['unsupported'] == 1
    assert out['stored'] == 0


def test_request_check_now_uses_execute_now(monkeypatch):
    client = FakeClient({
        'host.get': [{'hostid': '1', 'host': 'ATM-1'}],
        'item.get': [{'itemid': '9', 'hostid': '1', 'key_': 'atm.identity',
                      'lastvalue': '{}', 'lastclock': '0', 'state': '0',
                      'error': ''}],
        'task.create': {'taskids': ['1']},
    })
    monkeypatch.setattr(identity_sync, '_client', lambda: client)

    ok, error = identity_sync.request_check_now('ATM-1')

    assert ok is True
    assert error == ''
    task = [c for c in client.calls if c[0] == 'task.create']
    assert task and task[0][1]['type'] == 6
    assert task[0][1]['request']['itemid'] == '9'


def test_refresh_one_returns_the_new_value(monkeypatch):
    monkeypatch.setattr(identity_sync, '_client', lambda: object())
    monkeypatch.setattr(identity_sync, 'request_check_now', lambda *a, **k: (True, ''))
    monkeypatch.setattr(identity_sync, 'sync_one',
                        lambda atm_id, client=None: {'status': 'stored'})
    monkeypatch.setattr(identity_sync.time, 'sleep', lambda seconds: None)

    out = identity_sync.refresh_one('ATM-1', wait_seconds=1)

    assert out['status'] == 'stored'


def test_refresh_one_reports_a_refused_check(monkeypatch):
    monkeypatch.setattr(identity_sync, '_client', lambda: object())
    monkeypatch.setattr(identity_sync, 'request_check_now',
                        lambda *a, **k: (False, 'Super admin required'))

    out = identity_sync.refresh_one('ATM-1', wait_seconds=0)

    assert out['status'] == 'request_failed'
    assert 'Super admin' in out['error']
