#!/usr/bin/env python3
"""Create the Dashen Bank ATM Hardware - GRG XFS template and link to host via Zabbix API.

Single command end-to-end automation:
  1. Authenticates to Zabbix API using .env or environment variables.
  2. Creates or updates valuemaps (XFS Device, Safe Door, Dispenser, Cassette).
  3. Creates or updates the template 'Dashen Bank ATM Hardware - GRG XFS'.
  4. Configures master item 'atm.xfs.cdm' and all JavaScript preprocessing dependent items.
  5. Configures triggers for hardware fault, safe door open, cash levels, reject bin full.
  6. Links the template to the target host (e.g., TESTGRG at 10.41.30.115).

Usage (on UAT server):
    python3 scripts/import_grg_xfs_template_api.py [--host TESTGRG] [--ip 10.41.30.115]
"""
import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()

TEMPLATE_NAME = "Dashen Bank ATM Hardware - GRG XFS"
TEMPLATE_GROUP = "Templates"
DEFAULT_HOST = "TESTGRG"
DEFAULT_IP = "10.41.30.115"

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://172.22.10.45:8080/api_jsonrpc.php')
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


CASSETTES = [
    ("Rej01", "Reject Cassette 1", "reject", 0, "ETB"),
    ("Rct01", "Retract Cassette 2", "retract", 0, "ETB"),
    ("0050A", "Dispense Cassette 3 (ETB 50)", "dispense", 50, "ETB"),
    ("0100A", "Dispense Cassette 4 (ETB 100)", "dispense", 100, "ETB"),
    ("0100B", "Dispense Cassette 5 (ETB 100)", "dispense", 100, "ETB"),
    ("0200A", "Dispense Cassette 6 (ETB 200)", "dispense", 200, "ETB"),
]


def ensure_valuemaps(tid, auth):
    wanted = {
        "XFS Device Status": [
            (0, "Online"), (1, "Offline"), (2, "Power Off"), (3, "No Device"),
            (4, "Hardware Error"), (5, "User Error"), (6, "Busy"), (7, "Fraud Attempt")
        ],
        "XFS Safe Door Status": [
            (1, "Door Not Supported"), (2, "Door Open"), (3, "Door Closed"),
            (4, "Door Locked"), (5, "Door Unknown")
        ],
        "XFS Dispenser Status": [
            (0, "OK"), (1, "State Unknown"), (2, "Stop"), (3, "Unknown")
        ],
        "XFS Cassette Status": [
            (0, "OK"), (1, "Full"), (2, "High"), (3, "Low"), (4, "Empty"),
            (5, "Inoperative"), (6, "Missing"), (7, "No Values Available"), (8, "No Reference Value")
        ]
    }
    existing = {vm['name']: vm['valuemapid'] for vm in
                call('valuemap.get', {'hostids': [tid], 'output': ['valuemapid', 'name']}, auth)}
    vm_ids = {}
    for name, mappings in wanted.items():
        if name in existing:
            vm_ids[name] = existing[name]
        else:
            maps = [{'value': str(v), 'newvalue': t} for v, t in mappings]
            vid = call('valuemap.create', {'hostid': tid, 'name': name, 'mappings': maps}, auth)['valuemapids'][0]
            vm_ids[name] = vid
            print(f'Created valuemap: {name} ({vid})')
    return vm_ids


def wanted_items():
    """Yields (name, key, value_type, js_param, valuemap_name, units, desc)"""
    # Master item (None for js_param)
    yield ("GRG CDM — full XFS status & cassette inventory", "atm.xfs.cdm", 4, None, None, None, "Master XFS JSON")

    # Statuses
    yield ("CDM — Device Status", "atm.xfs.cdm.device_status", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.device;", "XFS Device Status", None, "fwDevice")
    yield ("CDM — Safe Door Status", "atm.xfs.cdm.safe_door", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.safe_door;", "XFS Safe Door Status", None, "fwSafeDoor")
    yield ("CDM — Dispenser State", "atm.xfs.cdm.dispenser", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.dispenser;", "XFS Dispenser Status", None, "fwDispenser")
    yield ("CDM — Intermediate Stacker", "atm.xfs.cdm.stacker", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.intermediate_stacker;", None, None, "fwStacker")

    # Summaries
    yield ("CDM Summary — Total Dispensable Cash", "atm.xfs.cdm.total_amount", 0,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.total_amount;", None, "ETB", "Total currency value")
    yield ("CDM Summary — Total Notes", "atm.xfs.cdm.total_notes", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.total_notes;", None, None, "Total dispensable notes")
    yield ("CDM Summary — Reject Notes Count", "atm.xfs.cdm.reject_count", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.reject_count;", None, None, "Reject bin note count")
    yield ("CDM Summary — Retract Notes Count", "atm.xfs.cdm.retract_count", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.retract_count;", None, None, "Retract bin note count")
    yield ("CDM Summary — Has Low Cassette", "atm.xfs.cdm.has_low", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.has_low;", None, None, "Low threshold flag")
    yield ("CDM Summary — Has Empty Cassette", "atm.xfs.cdm.has_empty", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.summary.has_empty;", None, None, "Empty threshold flag")

    # Cassettes
    for cid, cname, ctype, denom, cur in CASSETTES:
        yield (f"Cassette {cid} — {cname} Count", f"atm.xfs.cdm.cassette.count[{cid}]", 3,
               f'var j = JSON.parse(value);\nreturn j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].count : 0;',
               None, None, f"Count for {cname}")
        yield (f"Cassette {cid} — {cname} Status", f"atm.xfs.cdm.cassette.status[{cid}]", 3,
               f'var j = JSON.parse(value);\nreturn j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].status : 5;',
               "XFS Cassette Status", None, f"Status for {cname}")
        if ctype == "dispense":
            yield (f"Cassette {cid} — {cname} Reject Count", f"atm.xfs.cdm.cassette.reject[{cid}]", 3,
                   f'var j = JSON.parse(value);\nreturn j.cdm.cassettes["{cid}"] ? j.cdm.cassettes["{cid}"].reject_count : 0;',
                   None, None, f"Reject count for {cname}")


def ensure_triggers(tid, auth):
    wanted = [
        ("CDM — Cash Dispenser Hardware Fault on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>0", 4),
        ("CDM — Safe Door Open on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.safe_door)=2", 4),
        ("CDM — Dispenser Stopped on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.dispenser)=2", 4),
        ("CDM — Out of Cash on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)=0", 5),
        ("CDM — Total Cash Low (< 50,000 ETB) on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)<50000 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.total_amount)>0", 3),
        ("CDM — Reject Cassette Nearly Full on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.reject_count)>=250", 3),
    ]
    for cid, cname, ctype, denom, cur in CASSETTES:
        if ctype == "dispense":
            wanted.append((f"CDM — Cassette {cid} ({denom} {cur}) Empty on {{HOST.NAME}}",
                           f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.cassette.count[{cid}])=0", 2))

    existing = {t['description']: t['triggerid'] for t in
                call('trigger.get', {'templateids': [tid], 'output': ['triggerid', 'description']}, auth)}

    for desc, expr, prio in wanted:
        if desc not in existing:
            call('trigger.create', {'description': desc, 'expression': expr, 'priority': prio, 'status': 0}, auth)
            print(f'Created trigger: {desc}')


def link_template_to_host(tid, host_name, host_ip, auth):
    hosts = call('host.get', {'filter': {'host': [host_name]}, 'output': ['hostid'], 'selectInterfaces': ['interfaceid', 'ip', 'port'], 'selectParentTemplates': ['templateid']}, auth)
    if not hosts:
        # Create host if missing
        groups = call('hostgroup.get', {'filter': {'name': ['Discovered hosts', 'Linux servers', 'Templates']}, 'output': ['groupid']}, auth)
        gid = groups[0]['groupid'] if groups else '2'
        hid = call('host.create', {
            'host': host_name,
            'name': host_name,
            'groups': [{'groupid': gid}],
            'templates': [{'templateid': tid}],
            'interfaces': [{
                'type': 1, 'main': 1, 'useip': 1,
                'ip': host_ip, 'dns': '', 'port': '10050'
            }]
        }, auth)['hostids'][0]
        print(f'Created and linked host {host_name} ({hid}) to {TEMPLATE_NAME}')
        return

    hid = hosts[0]['hostid']
    current_templates = hosts[0].get('parentTemplates', [])
    tids = {t['templateid'] for t in current_templates}
    if tid not in tids:
        new_templates = [{'templateid': t['templateid']} for t in current_templates] + [{'templateid': tid}]
        call('host.update', {'hostid': hid, 'templates': new_templates}, auth)
        print(f'Successfully linked {TEMPLATE_NAME} to existing host {host_name} ({hid})')
    else:
        print(f'Host {host_name} ({hid}) is already linked to {TEMPLATE_NAME}')


def main():
    parser = argparse.ArgumentParser(description="Import GRG XFS template and link to host via Zabbix API")
    parser.add_argument('--host', default=DEFAULT_HOST, help=f"Host name to link (default: {DEFAULT_HOST})")
    parser.add_argument('--ip', default=DEFAULT_IP, help=f"Host IP to configure if creating (default: {DEFAULT_IP})")
    args = parser.parse_args()

    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print(f'Authenticated to Zabbix API ({ZBX_URL}) as {ZBX_USER}')

    # Find or create template group
    groups = call('templategroup.get', {'filter': {'name': [TEMPLATE_GROUP]}, 'output': ['groupid']}, auth)
    if not groups:
        gid = call('templategroup.create', {'name': TEMPLATE_GROUP}, auth)['groupids'][0]
    else:
        gid = groups[0]['groupid']

    # Find or create template
    existing = call('template.get', {'filter': {'host': [TEMPLATE_NAME]}, 'output': ['templateid']}, auth)
    if existing:
        tid = existing[0]['templateid']
        print(f'Template exists: {TEMPLATE_NAME} ({tid}) — verifying items')
    else:
        tid = call('template.create', {'host': TEMPLATE_NAME, 'name': TEMPLATE_NAME,
                                       'groups': [{'groupid': gid}]}, auth)['templateids'][0]
        print(f'Created template {TEMPLATE_NAME} ({tid})')

    vm_ids = ensure_valuemaps(tid, auth)

    have = {i['key_']: i for i in call('item.get', {'templateids': [tid], 'output': ['itemid', 'key_']}, auth)}
    mid = None
    created = 0
    updated = 0

    for iname, key, vtype, js_code, vm_name, units, desc in wanted_items():
        vmid = vm_ids.get(vm_name) if vm_name else None
        if js_code is None:
            # Master item
            if key in have:
                mid = have[key]['itemid']
            else:
                item_payload = {
                    'name': iname, 'key_': key, 'hostid': tid,
                    'type': 0, 'value_type': 4, 'delay': '1m',
                    'history': '7d', 'trends': '0', 'description': desc
                }
                mid = call('item.create', item_payload, auth)['itemids'][0]
                created += 1
            continue

        # Dependent item
        item_payload = {
            'name': iname, 'key_': key, 'hostid': tid,
            'type': 18, 'value_type': vtype, 'delay': '0',
            'history': '7d', 'trends': '30d' if vtype in (0, 3) else '0',
            'master_itemid': mid,
            'description': desc,
            'preprocessing': [{
                'type': 21,  # JAVASCRIPT
                'params': js_code,
                'error_handler': 0,
                'error_handler_params': ''
            }]
        }
        if units:
            item_payload['units'] = units
        if vmid:
            item_payload['valuemapid'] = vmid

        if key in have:
            call('item.update', {'itemid': have[key]['itemid'], **item_payload}, auth)
            updated += 1
        else:
            call('item.create', item_payload, auth)
            created += 1

    print(f'Items synced: {created} created, {updated} updated.')
    ensure_triggers(tid, auth)

    if args.host:
        link_template_to_host(tid, args.host, args.ip, auth)

    call('user.logout', [], auth)
    print('Done!')
    return 0


if __name__ == '__main__':
    sys.exit(main())
