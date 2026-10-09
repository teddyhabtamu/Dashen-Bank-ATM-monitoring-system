#!/usr/bin/env python3
"""Create the Dashen Bank ATM WMI Parts template via the Zabbix API.

Why API instead of XML import: Zabbix 6.4's configuration.import rejects
our generated XML tag-by-tag (<date>, then root <groups>, then the
ZABBIX_AGENT type constant) while accepting the sibling SNMP templates —
chasing the schema one rejection at a time is slower than calling the API
with unambiguous numeric types. Single source of truth stays
scripts/generate_wmi_template.py (PARTS list imported from there).

Usage (UAT-01, svc-scripts identity):
    export ZBX_URL=http://172.22.10.45:8080/api_jsonrpc.php
    export ZBX_USER=svc-scripts
    export ZBX_PASS='<password>'
    python3 scripts/import_wmi_template_api.py [--recreate]

Idempotent by default: if the template exists, items are verified and
missing ones are added (nothing is deleted). --recreate deletes the
template first (only when it is not linked to any host — refuses
otherwise, so run it before linking).
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_wmi_template import PARTS, TEMPLATE_NAME
# Flat group — see generate_wmi_template.py (nested groups break references
# when the implied parent object doesn't exist).
TEMPLATE_GROUP = "Templates"

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', '')

_id = 0


def call(method, params, auth=None):
    global _id
    _id += 1
    payload = {'jsonrpc': '2.0', 'method': method, 'params': params, 'id': _id}
    if auth:
        payload['auth'] = auth
    r = requests.post(ZBX_URL, json=payload,
                      headers={'Content-Type': 'application/json-rpc'}, timeout=30)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(f"{method}: {data['error'].get('data', data['error'])}")
    return data['result']


def path_for(part, field):
    # Pure bracket notation: the dotted form ($.parts["X"].y) dies in
    # Zabbix's parser with "invalid control character" on spaced names.
    return f'$["parts"]["{part}"]["{field}"]'


def wanted_items():
    """(name, key, field) for master + all dependents, mirroring the XML."""
    yield ('ATM Parts — full WMI tree', 'atm.parts', None)
    for part, repl in PARTS:
        yield (f'ATM Part — {part} Status', f'atm.part.part_status[{part}]', ('part_status', part))
        if repl:
            yield (f'ATM Part — {part} Replenishment',
                   f'atm.part.replenishment_status[{part}]', ('replenishment_status', part))


def main():
    recreate = '--recreate' in sys.argv
    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print('Authenticated to Zabbix API')

    existing = call('template.get', {'filter': {'host': [TEMPLATE_NAME]},
                                     'output': ['templateid'],
                                     'selectHosts': ['hostid']}, auth)
    if existing and recreate:
        tid = existing[0]['templateid']
        if existing[0].get('hosts'):
            print(f'ERROR: template {tid} is linked to hosts — unlink first, refusing.')
            return 1
        call('template.delete', [tid], auth)
        print(f'Deleted stale template {tid}')
        existing = []

    if existing:
        tid = existing[0]['templateid']
        print(f'Template exists: {TEMPLATE_NAME} ({tid}) — verifying items')
    else:
        # NOTE: templates link to TEMPLATE groups (templategroup API), not host
        # groups — same numeric ID realm confusion burned two rounds: API-created
        # host groups return IDs that template.create rejects as unreferenceable.
        groups = call('templategroup.get', {'filter': {'name': [TEMPLATE_GROUP]},
                                           'output': ['groupid']}, auth)
        if not groups:
            print(f'ERROR: template group "{TEMPLATE_GROUP}" not found.')
            return 1
        gid = groups[0]['groupid']
        tid = call('template.create', {'host': TEMPLATE_NAME, 'name': TEMPLATE_NAME,
                                       'groups': [{'groupid': gid}]}, auth)['templateids'][0]
        print(f'Created template {TEMPLATE_NAME} ({tid})')

    have = {i['key_']: i for i in
            call('item.get', {'templateids': [tid],
                              'output': ['itemid', 'key_'],
                              'selectPreprocessing': ['type', 'params'],
                              'limit': 500}, auth)}
    mid = None
    made = 0
    fixed = 0
    for name, key, field in wanted_items():
        if key in have:
            if field is None:
                mid = have[key]['itemid']
                continue
            fname, part = field
            want = path_for(part, fname)
            cur = (have[key].get('preprocessing') or [{}])[0].get('params')
            if cur != want:
                call('item.update', {
                    'itemid': have[key]['itemid'],
                    'preprocessing': [{'type': 12, 'params': want,
                                       'error_handler': 0,
                                       'error_handler_params': ''}]}, auth)
                fixed += 1
            continue
        if field is None:
            mid = call('item.create', {
                'name': name, 'key_': key, 'hostid': tid,
                'type': 0, 'value_type': 4, 'delay': '5m',
                'history': '7d', 'trends': '0'}, auth)['itemids'][0]
        else:
            fname, part = field
            call('item.create', {
                'name': name, 'key_': key, 'hostid': tid,
                'type': 18, 'value_type': 3, 'delay': '0',
                'history': '7d', 'trends': '0',
                'master_itemid': mid,
                # Preprocessing type 12 = JSONPath (5 is Regular expression,
                # whose two-parameter shape is what the API just rejected).
                # 6.4 also mandates error_handler per step (0 = set error,
                # the default UI behavior).
                'preprocessing': [{'type': 12,
                                   'params': path_for(part, fname),
                                   'error_handler': 0,
                                   'error_handler_params': ''}]}, auth)
        made += 1
    print(f'Done: {made} items created, {fixed} paths repaired, '
          f'{len(have) - fixed} already present '
          f'({len(have) + made} total, want {1 + sum(1 + int(r) for _, r in PARTS)})')
    call('user.logout', [], auth)
    return 0


if __name__ == '__main__':
    sys.exit(main())
