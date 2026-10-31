"""The shared-template migration must be safe to rerun.

The real UAT failure was data-dependent: one host linked both vendor
templates. These tests exercise the API sequence with a fake, especially the
two things that make the migration safe: it removes the legacy item from both
vendor templates, and it only links hosts that already have an agent interface.
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))

import import_agent_template_api as imp  # noqa: E402


class FakeApi:
    def __init__(self, hosts=None):
        self.calls = []
        self.hosts = hosts or []
        self.shared_template_created = False
        self.shared_item_created = False
        self.legacy_items = {
            'Dashen Bank ATM WMI Parts': ['99'],
            'Dashen Bank ATM Hardware - GRG XFS': [],
        }

    def __call__(self, method, params, auth=None):
        self.calls.append((method, params))
        if method == 'templategroup.get':
            return [{'groupid': '1'}]
        if method == 'template.get':
            name = (params.get('filter') or {}).get('host', [''])[0]
            if name == imp.TEMPLATE_NAME:
                return [{'templateid': '10'}] if self.shared_template_created else []
            if name in self.legacy_items:
                return [{'templateid': '20' if 'WMI' in name else '21'}]
            return []
        if method == 'template.create':
            self.shared_template_created = True
            return {'templateids': ['10']}
        if method == 'item.get':
            tid = (params.get('templateids') or [''])[0]
            if tid == '10':
                return [{'itemid': '50'}] if self.shared_item_created else []
            name = 'Dashen Bank ATM WMI Parts' if tid == '20' else \
                'Dashen Bank ATM Hardware - GRG XFS'
            return [{'itemid': i} for i in self.legacy_items.get(name, [])]
        if method == 'item.create':
            self.shared_item_created = True
            return {'itemids': ['50']}
        if method == 'item.update':
            return {'itemids': ['50']}
        if method == 'item.delete':
            for name, ids in self.legacy_items.items():
                if params[0] in ids:
                    ids.remove(params[0])
            return {'itemids': params}
        if method == 'hostgroup.get':
            return [{'groupid': '30'}]
        if method == 'host.get':
            return self.hosts
        if method == 'host.update':
            return {'hostids': [params['hostid']]}
        if method == 'user.logout':
            return True
        raise AssertionError(f'unexpected API call: {method} {params}')


def test_the_migration_creates_the_shared_template_and_removes_both_legacy_items(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(imp, 'call', api)
    tid = imp.ensure_template(api)
    imp.ensure_item(tid, api)
    removed = sum(imp.remove_legacy_item(n, api) for n in imp.LEGACY_TEMPLATES)

    assert tid == '10'
    assert api.shared_template_created is True
    assert api.shared_item_created is True
    assert removed == 1
    deletes = [c for c in api.calls if c[0] == 'item.delete']
    assert deletes and deletes[0][1] == ['99']


def test_link_all_skips_hosts_without_an_agent_interface(monkeypatch):
    api = FakeApi(hosts=[
        {'hostid': '1', 'host': 'ATM-NCR', 'interfaces': [{'type': '2'}, {'type': '1'}],
         'parentTemplates': [{'templateid': '20'}]},
        {'hostid': '2', 'host': 'ATM-GRG', 'interfaces': [{'type': '2'}],
         'parentTemplates': [{'templateid': '21'}]},
    ])

    monkeypatch.setattr(imp, 'call', api)
    linked, skipped = imp.link_all(api, '10')

    assert (linked, skipped) == (1, 1)
    updates = [c for c in api.calls if c[0] == 'host.update']
    assert len(updates) == 1
    assert updates[0][1]['hostid'] == '1'
    sent = [t['templateid'] for t in updates[0][1]['templates']]
    assert sent == ['20', '10'], 'the existing template must be preserved'


def test_link_all_is_idempotent_for_an_already_linked_host(monkeypatch):
    api = FakeApi(hosts=[
        {'hostid': '1', 'host': 'ATM-NCR', 'interfaces': [{'type': '1'}],
         'parentTemplates': [{'templateid': '10'}]},
    ])
    monkeypatch.setattr(imp, 'call', api)

    linked, skipped = imp.link_all(api, '10')

    assert (linked, skipped) == (0, 0)
    assert not [c for c in api.calls if c[0] == 'host.update']
