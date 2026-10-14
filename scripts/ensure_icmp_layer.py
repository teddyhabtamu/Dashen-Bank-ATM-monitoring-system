#!/usr/bin/env python3
"""Ensure the ICMP ping layer on the SNMP templates (NCR + GRG).

Why: agent/SNMP silence cannot distinguish power-cut from agent-crash.
A server-side `icmpping` (no agent, no box changes, no credentials) splits
it: ping-dead + data-dead = machine gone; ping-alive + data-dead = box
powered, monitoring sick. Each gets its own problem with its own runbook.

Idempotent: existing items/triggers are verified, never duplicated.
Usage (UAT-01, svc-scripts identity):
    export ZBX_URL=http://172.22.10.45:8080/api_jsonrpc.php
    export ZBX_USER=svc-scripts
    export ZBX_PASS='<password>'
    python3 scripts/ensure_icmp_layer.py
"""
import os
import sys

import requests

# .env fills whatever the shell didn't export.
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', '')

TEMPLATES = [
    "Dashen Bank ATM Hardware",
    "Dashen Bank ATM Hardware - GRG",
]

ITEM_NAME = 'ATM Network — Ping'
ITEM_KEY = 'icmpping'
TRIGGER_DESC = 'ATM Network — Unreachable by ping'


def ping_trigger_expr(template):
    # Functional syntax (proven on this build for nodata/last()).
    return f'last(/{template}/icmpping)=0'


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


def ensure_template(tid, template, auth):
    items = call('item.get', {'templateids': [tid],
                              'filter': {'key_': [ITEM_KEY]},
                              'output': ['itemid']}, auth)
    if items:
        print(f'  [{template}] ping item present ({items[0]["itemid"]})')
    else:
        iid = call('item.create', {
            'name': ITEM_NAME, 'key_': ITEM_KEY, 'hostid': tid,
            'type': 3, 'value_type': 3, 'delay': '1m',
            'history': '7d', 'trends': '30d'}, auth)['itemids'][0]
        print(f'  [{template}] ping item created ({iid})')
    found = call('trigger.get', {'templateids': [tid],
                                 'filter': {'description': [TRIGGER_DESC]},
                                 'output': ['triggerid']}, auth)
    if found:
        print(f'  [{template}] ping trigger present ({found[0]["triggerid"]})')
        return
    try:
        nid = call('trigger.create', {
            'description': TRIGGER_DESC,
            'expression': ping_trigger_expr(template),
            'priority': 4, 'status': 0}, auth)['triggerids'][0]
        print(f'  [{template}] ping trigger created ({nid})')
    except RuntimeError as e:
        print(f'  [{template}] TRIGGER-FAIL: {e}')


def main():
    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print('Authenticated to Zabbix API')
    for template in TEMPLATES:
        found = call('template.get', {'filter': {'host': [template]},
                                      'output': ['templateid']}, auth)
        if not found:
            print(f'  [{template}] NOT FOUND — skipping')
            continue
        ensure_template(found[0]['templateid'], template, auth)
    call('user.logout', [], auth)
    return 0


if __name__ == '__main__':
    sys.exit(main())
