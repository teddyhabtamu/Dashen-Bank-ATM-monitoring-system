#!/usr/bin/env python3
"""Ensure the reachability layer: per-template TCP/ping checks that split
"machine gone" from "monitoring sick".

History (why this file changed shape twice — read before "simplifying"):
- v1 used icmpping on both templates. Bank networks drop ICMP as policy
  (the team telnets TCP ports instead), and the NAT forwards TCP ports,
  not protocols — both ping checks read 0 on live boxes. ICMP was
  withdrawn from the firewall ask, not added to it.
- v2 (this file): each template checks what is actually reachable from
  the server today. NCR: TCP connect to the forwarded agent port
  (Average severity — diagnostic context for humans, no auto-ticket: the
  data-nodata triggers already ticket real outages). GRG: icmpping kept
  (nothing is forwarded there at all; its firing ticket tracks the
  firewall ask itself and auto-resolves on landing).
- The v1 icmpping item/trigger on the NCR template is deleted by this
  script (exact key + description match only — nothing else touched).

Idempotent: existing objects verified, never duplicated.
Usage (no exports needed — .env fills the rest):
    python3 scripts/ensure_reachability_layer.py
"""
import os
import sys

import requests

from zbx_env import ensure_loaded as _ensure_env
_ensure_env()

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', '')

# template: (check kind, item name, item key, trigger description, priority)
# kind 'tcp'  -> net.tcp.service on an already-forwarded port (no new
#                firewall needed; Average = context, not a ticket).
# kind 'icmp' -> icmpping (only where nothing is forwarded; the firing
#                ticket tracks the firewall ask itself).
CHECKS = {
    "Dashen Bank ATM Hardware": (
        'tcp', 'ATM Network — Agent Port TCP', 'net.tcp.service[tcp,,10050]',
        'ATM Network — Agent port TCP unreachable', 2),
    "Dashen Bank ATM Hardware - GRG": (
        'icmp', 'ATM Network — Ping', 'icmpping',
        'ATM Network — Unreachable by ping', 4),
}

# v1 leftovers, removed from the NCR template only (exact match).
RETIRED_NCR = ('icmpping', 'ATM Network — Unreachable by ping')


def check_expr(template, key):
    # Functional syntax (proven on this build for nodata/last()).
    return f'last(/{template}/{key})=0'


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


def ensure_check(tid, template, auth):
    kind, iname, ikey, tdesc, prio = CHECKS[template]
    items = call('item.get', {'templateids': [tid],
                              'filter': {'key_': [ikey]},
                              'output': ['itemid']}, auth)
    if items:
        print(f'  [{template}] reachability item present ({items[0]["itemid"]})')
    else:
        iid = call('item.create', {
            'name': iname, 'key_': ikey, 'hostid': tid,
            'type': 3, 'value_type': 3, 'delay': '1m',
            'history': '7d', 'trends': '30d'}, auth)['itemids'][0]
        print(f'  [{template}] reachability item created ({iid})')
    found = call('trigger.get', {'templateids': [tid],
                                 'filter': {'description': [tdesc]},
                                 'output': ['triggerid']}, auth)
    if found:
        print(f'  [{template}] reachability trigger present ({found[0]["triggerid"]})')
        return
    try:
        nid = call('trigger.create', {
            'description': tdesc,
            'expression': check_expr(template, ikey),
            'priority': prio, 'status': 0}, auth)['triggerids'][0]
        print(f'  [{template}] reachability trigger created ({nid})')
    except RuntimeError as e:
        print(f'  [{template}] TRIGGER-FAIL: {e}')


def retire_v1_ncr(tid, auth):
    key, desc = RETIRED_NCR
    items = call('item.get', {'templateids': [tid],
                              'filter': {'key_': [key]},
                              'output': ['itemid']}, auth)
    if items:
        call('item.delete', [i['itemid'] for i in items], auth)
        print(f'  [NCR] retired v1 icmpping item(s)')
    found = call('trigger.get', {'templateids': [tid],
                                 'filter': {'description': [desc]},
                                 'output': ['triggerid']}, auth)
    if found:
        call('trigger.delete', [t['triggerid'] for t in found], auth)
        print(f'  [NCR] retired v1 ping trigger(s)')


def main():
    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print('Authenticated to Zabbix API')
    for template in CHECKS:
        found = call('template.get', {'filter': {'host': [template]},
                                      'output': ['templateid']}, auth)
        if not found:
            print(f'  [{template}] NOT FOUND — skipping')
            continue
        tid = found[0]['templateid']
        if template == "Dashen Bank ATM Hardware":
            retire_v1_ncr(tid, auth)
        ensure_check(tid, template, auth)
    call('user.logout', [], auth)
    return 0


if __name__ == '__main__':
    sys.exit(main())
