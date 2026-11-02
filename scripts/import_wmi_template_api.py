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
# .env fills whatever the shell didn't export.
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()
from generate_wmi_template import PARTS, DEVICES, TEMPLATE_NAME
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
    # Kept for reference/back-compat only. Zabbix 6.4's JSONPath cannot
    # address spaced keys in ANY bracket form (proven live: single AND
    # double quotes die with "invalid control character" at the first
    # space). Dependents use JavaScript preprocessing instead (see below).
    return f'$["parts"]["{part}"]["{field}"]'


def js_for(tree, name, field):
    return (f'var j = JSON.parse(value);\n'
            f'return j.{tree}["{name}"].{field};')


def wanted_items():
    """(name, key, field) for master + all dependents, mirroring the XML.

    field is (tree, object-name, leaf) — parts live under j.parts,
    devices under j.devices.
    """
    yield ('ATM Parts — full WMI tree', 'atm.parts', None)
    for part, repl in PARTS:
        yield (f'ATM Part — {part} Status', f'atm.part.part_status[{part}]',
               ('parts', part, 'part_status'))
        if repl:
            yield (f'ATM Part — {part} Replenishment',
                   f'atm.part.replenishment_status[{part}]',
                   ('parts', part, 'replenishment_status'))
    for dev in DEVICES:
        yield (f'ATM Device — {dev} Status', f'atm.device.status[{dev}]',
               ('devices', dev, 'device_status'))


def ensure_proven_triggers(tid, auth):
    # v1 proven triggers (vendor legends from NCR_DevDevicePart qualifiers
    # 2026-10-09 and NCR_DevDevice.DeviceStatus 2026-10-12 — zero guessing):
    #   part_status == 2 (Error), replenishment_status == 2 (Error),
    #   device_status == 2 (Unavailable).
    # Deliberately NOT triggering: 1 Warning (noise level unknown — v2),
    # 3/4/5 Not Configured/Unknown/Accessed (test core sits at Unknown on
    # cassettes by configuration, not by fault), PM (no items yet — v2).
    # Severity High: live proof fires immediately (Transport (2), Paper
    # replenishment, RPNT Unavailable) — expect problems + GLPI test tickets.
    wanted = []
    for part, repl in PARTS:
        wanted.append(
            (f'ATM Part — {part} ERROR',
             f'last(/{TEMPLATE_NAME}/atm.part.part_status[{part}])=2'))
        if repl:
            wanted.append(
                (f'ATM Part — {part} REPLENISHMENT ERROR',
                 f'last(/{TEMPLATE_NAME}/atm.part.replenishment_status[{part}])=2'))
    for dev in DEVICES:
        wanted.append(
            (f'ATM Device — {dev} UNAVAILABLE',
             f'last(/{TEMPLATE_NAME}/atm.device.status[{dev}])=2'))
    have = {t['description']: t['triggerid'] for t in
            call('trigger.get', {'templateids': [tid],
                                 'output': ['triggerid', 'description'],
                                 'limit': 500}, auth)}
    made, failed = 0, []
    for desc, expr in wanted:
        if desc in have:
            continue
        try:
            call('trigger.create', {'description': desc, 'expression': expr,
                                    'priority': 4, 'status': 0}, auth)
            made += 1
        except RuntimeError as e:
            failed.append(f'{desc}: {e}')
    print(f'Proven triggers: {made} created, {len(have)} already present, '
          f'{len(failed)} failed')
    for f in failed:
        print(f'  TRIGGER-FAIL: {f}')


def ensure_master_nodata_trigger(tid, auth):
    # Semantics-free health only: fires when the collector goes blind.
    # Deliberately the ONLY trigger here — value thresholds on part_status /
    # replenishment_status are uncalibrated guesses until RDP fault drills
    # prove them (see runbook). Guesses live in docs as UNPROVEN, never as
    # triggers (disabled or otherwise): a disabled guess still becomes a page
    # the day anyone enables template triggers wholesale.
    desc = 'ATM Parts collector — no data for 10m (agent/collector down)'
    found = call('trigger.get', {'templateids': [tid],
                                 'filter': {'description': [desc]},
                                 'output': ['triggerid']}, auth)
    if found:
        print(f'Health trigger present ({found[0]["triggerid"]})')
        return
    # 6.4 trigger.create speaks the functional syntax (last(/host/key),
    # nodata(/host/key,10m)) — the old {host:key.func()} brace form dies at
    # char 0 (proven live: nodata AND last() both rejected). Doc example:
    # "last(/Linux server/system.cpu.load[percpu,avg1])>5".
    expr = 'nodata(/%s/atm.parts,10m)=1' % TEMPLATE_NAME
    nid = call('trigger.create', {'description': desc,
                                  'expression': expr,
                                  'priority': 2,
                                  'status': 0}, auth)['triggerids'][0]
    print(f'Health trigger created ({nid})')


def remove_legacy_identity_item(tid, auth):
    """Remove `atm.identity` from this vendor template.

    It used to live here. It now belongs to the shared "Dashen Bank ATM Agent"
    template; leaving a copy here makes any host that links two vendor
    templates inherit the same key twice, which Zabbix rejects. Idempotent:
    the item is usually already gone.
    """
    found = call('item.get', {'templateids': [tid],
                              'filter': {'key_': ['atm.identity']},
                              'output': ['itemid']}, auth)
    for item in found:
        call('item.delete', [item['itemid']], auth)
        print(f'Removed legacy identity item ({item["itemid"]})')
    if not found:
        print('No legacy identity item on this template')


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
    for iname, key, field in wanted_items():
        if key in have:
            if field is None:
                mid = have[key]['itemid']
                # Keep an existing deployment in step with the state pipeline:
                # a 5-minute WMI tree made a real NCR fault take up to six
                # minutes to reach the portal. The WMI query also needs more
                # than Zabbix's 3s default; the server/proxy global Timeout
                # covers it (compose sets ZBX_TIMEOUT=15).
                call('item.update', {'itemid': mid, 'delay': '1m'}, auth)
                continue
            tree, obj, leaf = field
            # Converge every dependent to the JavaScript step (see above):
            # anything else on these keys is a leftover of the JSONPath era.
            call('item.update', {
                'itemid': have[key]['itemid'],
                'preprocessing': [{'type': 21, 'params': js_for(tree, obj, leaf),
                                   'error_handler': 0,
                                   'error_handler_params': ''}]}, auth)
            fixed += 1
            continue
        if field is None:
            mid = call('item.create', {
                'name': iname, 'key_': key, 'hostid': tid,
                'type': 0, 'value_type': 4, 'delay': '1m',
                'history': '7d', 'trends': '0'}, auth)['itemids'][0]
        else:
            tree, obj, leaf = field
            call('item.create', {
                'name': iname, 'key_': key, 'hostid': tid,
                'type': 18, 'value_type': 3, 'delay': '0',
                'history': '7d', 'trends': '0',
                'master_itemid': mid,
                'preprocessing': [{'type': 21,
                                   'params': js_for(tree, obj, leaf),
                                   'error_handler': 0,
                                   'error_handler_params': ''}]}, auth)
        made += 1
    want_total = 1 + sum(1 + int(r) for _, r in PARTS) + len(DEVICES)
    print(f'Done: {made} items created, {fixed} paths repaired, '
          f'{len(have) - fixed} already present '
          f'({len(have) + made} total, want {want_total})')
    ensure_master_nodata_trigger(tid, auth)
    ensure_proven_triggers(tid, auth)
    remove_legacy_identity_item(tid, auth)
    call('user.logout', [], auth)
    return 0


if __name__ == '__main__':
    sys.exit(main())
