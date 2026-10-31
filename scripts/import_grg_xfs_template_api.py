#!/usr/bin/env python3
"""Create the Dashen Bank ATM Hardware - GRG XFS template and link to host via Zabbix API.

Single command end-to-end automation:
  1. Authenticates to Zabbix API using .env or environment variables.
  2. Creates or updates valuemaps (XFS Device, Safe Door, Dispenser, Cassette).
  3. Creates or updates the template 'Dashen Bank ATM Hardware - GRG XFS'.
  4. Configures master item 'atm.xfs.cdm' and all JavaScript preprocessing dependent items.
  5. Configures triggers for hardware fault, safe door open, cash levels, reject bin full.
  6. Links the template to the target host (e.g., TESTGRG).

Usage (on UAT server):
    ZBX_URL=<zabbix-api-url> \
        python3 scripts/import_grg_xfs_template_api.py [--host TESTGRG]
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

ZBX_SCHEME = os.environ.get('ZBX_SCHEME', 'http')
# APPS_VM_IP is the documented UAT-01 address; keeping it as a fallback
# preserves the old default without embedding an IP in the source.
ZBX_HOST = os.environ.get('ZBX_HOST') or os.environ.get('APPS_VM_IP') or 'localhost'
ZBX_PORT = os.environ.get('ZBX_PORT', '8080')
ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get('ZABBIX_URL') or (
    f'{ZBX_SCHEME}://{ZBX_HOST}:{ZBX_PORT}/api_jsonrpc.php')
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
            (4, "Hardware Error"), (5, "User Error"), (6, "Busy"), (7, "Fraud Attempt"), (255, "Not queried")
        ],
        "XFS Safe Door Status": [
            (1, "Door Not Supported"), (2, "Door Open"), (3, "Door Closed"),
            (4, "Door Locked"), (5, "Door Unknown"), (255, "Not queried")
        ],
        "XFS Dispenser Status": [
            (0, "OK"), (1, "State Unknown"), (2, "Stop"), (3, "Unknown")
        ],
        "XFS Cassette Status": [
            (0, "OK"), (1, "Full"), (2, "High"), (3, "Low"), (4, "Empty"),
            (5, "Inoperative"), (6, "Missing"), (7, "No Values Available"), (8, "No Reference Value")
        ],
        "XFS IDC Media Status": [
            (1, "Media Present"), (2, "Media Not Present"), (3, "Media Jammed"),
            (4, "Not Supported"), (5, "Unknown"), (255, "Not queried")
        ],
        "XFS IDC Retain Bin": [
            (1, "OK"), (2, "Full"), (3, "High"), (4, "Not Supported"), (255, "Not queried")
        ],
        "XFS PTR Paper Status": [
            (0, "Full / OK"), (1, "Paper Low"), (2, "Paper Out"), (3, "Not Supported"), (4, "Unknown"), (255, "Not queried")
        ]
    }
    existing = {vm['name']: vm['valuemapid'] for vm in
                call('valuemap.get', {'hostids': [tid], 'output': ['valuemapid', 'name']}, auth)}
    vm_ids = {}
    for name, mappings in wanted.items():
        maps = [{'value': str(v), 'newvalue': t} for v, t in mappings]
        if name in existing:
            vm_ids[name] = existing[name]
            call('valuemap.update', {'valuemapid': existing[name], 'mappings': maps}, auth)
        else:
            vid = call('valuemap.create', {'hostid': tid, 'name': name, 'mappings': maps}, auth)['valuemapids'][0]
            vm_ids[name] = vid
            print(f'Created valuemap: {name} ({vid})')
    return vm_ids


def wanted_items():
    """Yields (name, key, value_type, js_param, valuemap_name, units, desc)"""
    # Master item (None for js_param)
    yield ("GRG XFS — full status & cassette inventory", "atm.xfs.cdm", 4, None, None, None, "Master XFS JSON (CDM + IDC/PTR/PIN/SIU status)")

    # CDM Statuses
    yield ("CDM — Device Status", "atm.xfs.cdm.device_status", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.device;", "XFS Device Status", None, "fwDevice")
    yield ("CDM — Safe Door Status", "atm.xfs.cdm.safe_door", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.safe_door;", "XFS Safe Door Status", None, "fwSafeDoor")
    yield ("CDM — Dispenser State", "atm.xfs.cdm.dispenser", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.dispenser;", "XFS Dispenser Status", None, "fwDispenser")
    yield ("CDM — Intermediate Stacker", "atm.xfs.cdm.stacker", 3,
           "var j = JSON.parse(value);\nreturn j.cdm.status.intermediate_stacker;", None, None, "fwStacker")

    # CDM Summaries
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

    # IDC (Card Reader)
    yield ("Card Reader — Device Status", "atm.xfs.idc.device_status", 3,
           "var j = JSON.parse(value);\nreturn (j.idc && j.idc.device !== undefined) ? j.idc.device : 255;", "XFS Device Status", None, "IDC fwDevice")
    yield ("Card Reader — Media Status", "atm.xfs.idc.media_status", 3,
           "var j = JSON.parse(value);\nreturn (j.idc && j.idc.media !== undefined) ? j.idc.media : 255;", "XFS IDC Media Status", None, "Card present / jammed / empty")
    yield ("Card Reader — Retained Cards Bin", "atm.xfs.idc.retain_bin", 3,
           "var j = JSON.parse(value);\nreturn (j.idc && j.idc.retain_bin !== undefined) ? j.idc.retain_bin : 255;", "XFS IDC Retain Bin", None, "Retain bin full/ok")
    yield ("Card Reader — Retained Cards Count", "atm.xfs.idc.cards", 3,
           "var j = JSON.parse(value);\nreturn (j.idc && j.idc.cards !== undefined) ? j.idc.cards : 0;", None, None, "Captured card count")

    # PTR (Receipt Printer)
    yield ("Printer — Device Status", "atm.xfs.ptr.device_status", 3,
           "var j = JSON.parse(value);\nreturn (j.ptr && j.ptr.device !== undefined) ? j.ptr.device : 255;", "XFS Device Status", None, "PTR fwDevice")
    yield ("Printer — Paper Status", "atm.xfs.ptr.paper_status", 3,
           "var j = JSON.parse(value);\nreturn (j.ptr && j.ptr.paper !== undefined) ? j.ptr.paper : 255;", "XFS PTR Paper Status", None, "Paper full / low / out")

    # PIN (PIN Pad / EPP)
    yield ("PIN Pad — Device Status", "atm.xfs.pin.device_status", 3,
           "var j = JSON.parse(value);\nreturn (j.pin && j.pin.device !== undefined) ? j.pin.device : 255;", "XFS Device Status", None, "PIN fwDevice")

    # SIU (Sensors & Enclosures)
    yield ("SIU — Cabinet Door Status", "atm.xfs.siu.cabinet_door", 3,
           "var j = JSON.parse(value);\nreturn (j.siu && j.siu.cabinet_door !== undefined) ? j.siu.cabinet_door : 255;", "XFS Safe Door Status", None, "Upper fascia / operator door")
    yield ("SIU — Safe Door Status", "atm.xfs.siu.safe_door", 3,
           "var j = JSON.parse(value);\nreturn (j.siu && j.siu.safe_door !== undefined) ? j.siu.safe_door : 255;", "XFS Safe Door Status", None, "Lower safe / vault door")
    yield ("SIU — Supervisor Mode Switch", "atm.xfs.siu.supervisor_mode", 3,
           "var j = JSON.parse(value);\nreturn (j.siu && j.siu.supervisor_switch !== undefined) ? j.siu.supervisor_switch : 255;", None, None, "0=Normal Run, 1=Supervisor")
    yield ("SIU — Tamper Alarm Sensor", "atm.xfs.siu.tamper", 3,
           "var j = JSON.parse(value);\nreturn (j.siu && j.siu.tamper_sensor !== undefined) ? j.siu.tamper_sensor : 255;", None, None, "0=OK, 1=Tamper Alarm")


def ensure_triggers(tid, auth):
    wanted = [
        ("CDM — Cash Dispenser Hardware Fault on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.cdm.device_status)<>255", 4),
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
        ("Card Reader — Hardware Error on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.idc.device_status)<>255", 4),
        ("Card Reader — Card Jammed on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.idc.media_status)=3", 4),
        ("Printer — Hardware Error on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.ptr.device_status)<>255", 4),
        ("Printer — Paper Out on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.paper_status)=2", 3),
        ("Printer — Paper Low on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.ptr.paper_status)=1", 2),
        ("PIN Pad — Hardware Error on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>0 and last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>6 and last(/{TEMPLATE_NAME}/atm.xfs.pin.device_status)<>255", 4),
        ("SIU — Cabinet / Fascia Door Open on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.siu.cabinet_door)=2", 3),
        ("SIU — Supervisor / Maintenance Mode on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.siu.supervisor_mode)=1", 2),
        ("SIU — Tamper Alarm on {HOST.NAME}",
         f"last(/{TEMPLATE_NAME}/atm.xfs.siu.tamper)=1", 5),
    ]
    for cid, cname, ctype, denom, cur in CASSETTES:
        if ctype == "dispense":
            wanted.append((f"CDM — Cassette {cid} ({denom} {cur}) Empty on {{HOST.NAME}}",
                           f"last(/{TEMPLATE_NAME}/atm.xfs.cdm.cassette.count[{cid}])=0", 2))

    existing = {t['description']: t['triggerid'] for t in
                call('trigger.get', {'templateids': [tid], 'output': ['triggerid', 'description']}, auth)}

    for desc, expr, prio in wanted:
        if desc in existing:
            call('trigger.update', {'triggerid': existing[desc], 'expression': expr,
                                    'priority': prio, 'status': 0}, auth)
        else:
            call('trigger.create', {'description': desc, 'expression': expr, 'priority': prio, 'status': 0}, auth)
            print(f'Created trigger: {desc}')


def link_template_to_host(tid, target_name, host_ip, auth):
    # Try finding by technical host name or visible name
    hosts = call('host.get', {
        'filter': {'host': [target_name, 'ATM-TEST-GRG', '4 Kilo Test GRG']},
        'output': ['hostid', 'host', 'name'],
        'selectParentTemplates': ['templateid', 'name']
    }, auth)
    
    # If not found by filter, search all hosts by name
    if not hosts:
        all_hosts = call('host.get', {'output': ['hostid', 'host', 'name'], 'selectParentTemplates': ['templateid', 'name']}, auth)
        hosts = [h for h in all_hosts if h['name'] == target_name or h['host'] == target_name or '4 Kilo Test GRG' in (h['name'], h['host'])]

    # Delete temporary standalone TESTGRG host if it exists and is not the primary portal host
    temp_hosts = call('host.get', {'filter': {'host': ['TESTGRG']}, 'output': ['hostid', 'host', 'name']}, auth)
    for th in temp_hosts:
        if th['name'] != '4 Kilo Test GRG' and th['host'] != 'ATM-TEST-GRG':
            try:
                call('host.delete', [th['hostid']], auth)
                print(f"Cleaned up temporary standalone host {th['host']} ({th['hostid']})")
            except Exception as e:
                print(f"Note: could not delete temporary host: {e}")

    if not hosts:
        print(f"ERROR: Could not find primary host {target_name} or '4 Kilo Test GRG'")
        return

    target = hosts[0]
    hid = target['hostid']
    h_display = f"{target['name']} [{target['host']}]"
    
    current_templates = target.get('parentTemplates', [])
    tids = {t['templateid'] for t in current_templates}
    
    if tid not in tids:
        # Keep existing templates, just append the new XFS template
        new_templates = [{'templateid': t['templateid']} for t in current_templates] + [{'templateid': tid}]
        call('host.update', {'hostid': hid, 'templates': new_templates}, auth)
        print(f"Successfully linked {TEMPLATE_NAME} to portal host {h_display} ({hid})")
    else:
        print(f"Portal host {h_display} ({hid}) is already linked to {TEMPLATE_NAME}")


def ensure_identity_item(tid, auth):
    """Create/update the passive `atm.identity` item on the GRG XFS template.

    GRG hosts that link this template already need an agent interface for the
    XFS items; identity uses the same path. Hosts that are SNMP-only will not
    have this item, and the portal reports it as missing rather than inventing
    an identity.
    """
    key = 'atm.identity'
    payload = {
        'name': 'ATM identity — JSON evidence',
        'key_': key,
        'hostid': tid,
        'type': 0,           # ZABBIX_AGENT
        'value_type': 4,     # TEXT
        'delay': '1h',
        'history': '1d',
        'trends': '0',
        'description': ('Read-only JSON from C:\\monitor\\atm-identity.ps1. '
                        'The portal ingests it automatically; it is evidence, '
                        'never authority.'),
    }
    found = call('item.get', {'templateids': [tid],
                              'filter': {'key_': [key]},
                              'output': ['itemid']}, auth)
    if found:
        upd = {k: v for k, v in payload.items() if k != 'hostid'}
        call('item.update', {'itemid': found[0]['itemid'], **upd}, auth)
        print(f'Identity item present ({found[0]["itemid"]})')
    else:
        call('item.create', payload, auth)
        print('Identity item created')


def main():
    parser = argparse.ArgumentParser(description="Import GRG XFS template and link to host via Zabbix API")
    parser.add_argument('--host', default=DEFAULT_HOST, help=f"Host name to link (default: {DEFAULT_HOST})")
    parser.add_argument('--ip', default=os.environ.get('GRG_TEST_IP') or None,
                        help='Deprecated; ignored. Configure the host interface in Zabbix.')
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
            upd_payload = {k: v for k, v in item_payload.items() if k != 'hostid'}
            call('item.update', {'itemid': have[key]['itemid'], **upd_payload}, auth)
            updated += 1
        else:
            call('item.create', item_payload, auth)
            created += 1

    print(f'Items synced: {created} created, {updated} updated.')
    ensure_triggers(tid, auth)
    ensure_identity_item(tid, auth)

    if args.host:
        link_template_to_host(tid, args.host, args.ip, auth)

    call('user.logout', [], auth)
    print('Done!')
    return 0


if __name__ == '__main__':
    sys.exit(main())
