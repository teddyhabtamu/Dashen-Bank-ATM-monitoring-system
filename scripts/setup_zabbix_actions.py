#!/usr/bin/env python3
"""
Zabbix Trigger-Action setup for Dashen Bank ATM Monitoring.

Zabbix 6.4 can export templates, hosts and media types to XML, but it
CANNOT export/import trigger actions. This script recreates the action
that links ATM triggers to the "GLPI Ticket" media type, so incident
tickets are auto-created (and moved to Pending / RCA on recovery).

It is idempotent: if an enabled trigger action already sends via the
GLPI media type, it does nothing.

Env vars (with sensible local defaults):
    ZABBIX_URL   default http://localhost:8080/api_jsonrpc.php
    ZABBIX_USER  default Admin
    ZABBIX_PASS  default zabbix
"""
import os
import sys
import requests

ZBX_URL  = os.environ.get('ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZABBIX_PASS', 'zabbix')

MEDIA_TYPE_NAME = 'GLPI Ticket'
ACTION_NAME     = 'ATM GLPI Auto-Ticket'
GLPI_SENDTO     = 'http://glpi:80/apirest.php'

# Trigger severity >= High (Zabbix: 0 NotClassified .. 5 Disaster)
MIN_SEVERITY = '4'

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


def main():
    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    print('Authenticated to Zabbix API')

    mts = call('mediatype.get',
               {'output': ['mediatypeid', 'name'],
                'filter': {'name': MEDIA_TYPE_NAME}}, auth)
    if not mts:
        print(f'ERROR: media type "{MEDIA_TYPE_NAME}" not found. '
              f'Import config/zabbix/zbx_export_mediatypes.xml first.')
        sys.exit(1)
    mtid = mts[0]['mediatypeid']
    print(f'Found media type "{MEDIA_TYPE_NAME}" (id {mtid})')

    # 1. Ensure the admin user has the GLPI media (required to send).
    users = call('user.get',
                 {'output': ['userid', 'username'],
                  'selectMedias': ['mediatypeid', 'sendto'],
                  'filter': {'username': ZBX_USER}}, auth)
    user = users[0]
    uid = user['userid']
    medias = user.get('medias', [])
    if not any(m['mediatypeid'] == mtid for m in medias):
        medias.append({'mediatypeid': mtid, 'sendto': GLPI_SENDTO,
                       'active': '0', 'severity': '63', 'period': '1-7,00:00-24:00'})
        call('user.update', {'userid': uid, 'medias': medias}, auth)
        print(f'Added GLPI media to user "{ZBX_USER}"')
    else:
        print(f'User "{ZBX_USER}" already has GLPI media')

    # 2. Skip if any enabled trigger action already sends via GLPI media type.
    actions = call('action.get',
                   {'output': ['actionid', 'name', 'status'],
                    'selectOperations': 'extend',
                    'filter': {'eventsource': 0}}, auth)
    for a in actions:
        if a['status'] != '0':
            continue
        for op in a.get('operations', []):
            if str(op.get('opmessage', {}).get('mediatypeid')) == str(mtid):
                print(f'Action "{a["name"]}" (id {a["actionid"]}) already '
                      f'wires triggers to GLPI — nothing to do.')
                call('user.logout', [], auth)
                return

    # 3. Create the action.
    result = call('action.create', {
        'name': ACTION_NAME,
        'eventsource': 0,
        'status': 0,
        'esc_period': '1h',
        'pause_suppressed': '1',
        'notify_if_canceled': '1',
        'filter': {
            'evaltype': 0,
            'conditions': [
                {'conditiontype': 4, 'operator': 5, 'value': MIN_SEVERITY},
            ],
        },
        'operations': [
            {'operationtype': 0, 'esc_step_from': 1, 'esc_step_to': 1,
             'opmessage': {'default_msg': 1, 'mediatypeid': mtid},
             'opmessage_usr': [{'userid': uid}]},
        ],
        'recovery_operations': [
            {'operationtype': 0,
             'opmessage': {'default_msg': 1, 'mediatypeid': mtid},
             'opmessage_usr': [{'userid': uid}]},
        ],
    }, auth)
    print(f'Created action "{ACTION_NAME}" (id {result["actionids"][0]}) '
          f'— triggers of severity >= High now auto-create GLPI tickets.')
    call('user.logout', [], auth)


if __name__ == '__main__':
    main()
