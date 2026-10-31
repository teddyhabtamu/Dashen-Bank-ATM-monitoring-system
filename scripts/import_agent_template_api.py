#!/usr/bin/env python3
"""Create the shared ATM agent template and move `atm.identity` onto it.

Why this exists: the identity item was added to both the NCR WMI template and
the GRG XFS template. Zabbix allows that only while no host links both. The
UAT host did link both, so the GRG import failed with:

    Cannot inherit item with key "atm.identity" ... already inherited from
    template "Dashen Bank ATM WMI Parts"

A shared template removes the data-dependent failure. Vendor templates keep
vendor telemetry; this one owns the common agent contract.

Usage (UAT-01, svc-scripts identity):
    export ZBX_URL=http://172.22.10.45:8080/api_jsonrpc.php
    export ZBX_USER=svc-scripts
    export ZBX_PASS='<password>'
    python3 scripts/import_agent_template_api.py --link-all

`--link-all` links the template to hosts in the `ATMs` group that already have
an agent interface. Hosts without one are left for zabbix_sync to repair; it
adds the interface and links the template in the same pass.
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbx_env import ensure_loaded as _ensure_env
_ensure_env()
from generate_agent_template import (IDENTITY_KEY, TEMPLATE_GROUP,
                                     TEMPLATE_NAME)

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', '')

# Templates that used to own the identity item. The migration removes it from
# them so a host can never inherit the same key twice.
LEGACY_TEMPLATES = (
    'Dashen Bank ATM WMI Parts',
    'Dashen Bank ATM Hardware - GRG XFS',
)

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


def ensure_template(auth):
    groups = call('templategroup.get',
                  {'filter': {'name': [TEMPLATE_GROUP]}, 'output': ['groupid']},
                  auth)
    if groups:
        gid = groups[0]['groupid']
    else:
        gid = call('templategroup.create', {'name': TEMPLATE_GROUP}, auth)['groupids'][0]

    found = call('template.get', {'filter': {'host': [TEMPLATE_NAME]},
                                  'output': ['templateid']}, auth)
    if found:
        tid = found[0]['templateid']
        print(f'Template exists: {TEMPLATE_NAME} ({tid})')
        return tid
    tid = call('template.create', {'host': TEMPLATE_NAME, 'name': TEMPLATE_NAME,
                                   'groups': [{'groupid': gid}]}, auth)['templateids'][0]
    print(f'Created template {TEMPLATE_NAME} ({tid})')
    return tid


def ensure_item(tid, auth):
    payload = {
        'name': 'ATM identity — JSON evidence',
        'key_': IDENTITY_KEY,
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
                              'filter': {'key_': [IDENTITY_KEY]},
                              'output': ['itemid']}, auth)
    if found:
        call('item.update', {'itemid': found[0]['itemid'], **payload}, auth)
        print(f'Identity item present ({found[0]["itemid"]})')
        return
    call('item.create', {'hostid': tid, **payload}, auth)
    print('Identity item created')


def remove_legacy_item(template_name, auth):
    found = call('template.get', {'filter': {'host': [template_name]},
                                  'output': ['templateid']}, auth)
    if not found:
        print(f'Legacy template not found (skip): {template_name}')
        return 0
    tid = found[0]['templateid']
    items = call('item.get', {'templateids': [tid],
                              'filter': {'key_': [IDENTITY_KEY]},
                              'output': ['itemid']}, auth)
    for item in items:
        call('item.delete', [item['itemid']], auth)
        print(f'Removed {IDENTITY_KEY} from {template_name} ({item["itemid"]})')
    return len(items)


def link_all(auth, agent_tid):
    """Link the shared template to ATM-group hosts that have an agent interface.

    `host.update` replaces the template set, so send the union. Hosts without
    an agent interface are counted and left alone: linking the template there
    would only make every item unsupported until zabbix_sync adds the
    interface.
    """
    groups = call('hostgroup.get', {'filter': {'name': ['ATMs']},
                                    'output': ['groupid']}, auth)
    if not groups:
        print('Host group ATMs not found; nothing linked.')
        return 0, 0
    gid = groups[0]['groupid']
    hosts = call('host.get', {
        'groupids': [gid],
        'output': ['hostid', 'host'],
        'selectInterfaces': ['type'],
        'selectParentTemplates': ['templateid'],
    }, auth)
    linked = skipped = 0
    for host in hosts:
        if not any(str(i.get('type')) == '1' for i in host.get('interfaces') or []):
            skipped += 1
            continue
        have = [t['templateid'] for t in host.get('parentTemplates') or []
                if t.get('templateid')]
        if agent_tid in have:
            continue
        call('host.update', {
            'hostid': host['hostid'],
            'templates': [{'templateid': t} for t in (have + [agent_tid])],
        }, auth)
        linked += 1
        print(f'Linked {TEMPLATE_NAME} to {host.get("host")}')
    return linked, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--link-all', action='store_true',
                        help='Link the template to ATM-group hosts with an agent interface')
    args = parser.parse_args(argv)

    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print(f'Authenticated to Zabbix API ({ZBX_URL}) as {ZBX_USER}')

    tid = ensure_template(auth)
    ensure_item(tid, auth)

    removed = 0
    for name in LEGACY_TEMPLATES:
        removed += remove_legacy_item(name, auth)
    if removed:
        print(f'Migration removed {removed} legacy identity item(s).')

    if args.link_all:
        linked, skipped = link_all(auth, tid)
        print(f'Linked to {linked} host(s); {skipped} host(s) have no agent interface yet.')
        if skipped:
            print('Run zabbix_sync to add the interface and link those hosts.')

    call('user.logout', [], auth)
    return 0


if __name__ == '__main__':
    sys.exit(main())
