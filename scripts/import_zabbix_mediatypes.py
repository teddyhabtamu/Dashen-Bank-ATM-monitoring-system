#!/usr/bin/env python3
"""Import Zabbix media types (GLPI Ticket webhook) with live secrets.

Reads config/zabbix/zbx_export_mediatypes.xml, substitutes the
__GLPI_*__ placeholders from the environment (.env), and imports via
configuration.import (idempotent: updateExisting=true).

Env: ZBX_URL, ZBX_USER, ZBX_PASS, GLPI_APP_TOKEN, GLPI_API_PASSWORD, GLPI_USER.
"""
import os
import sys
import requests

ZBX_URL = os.environ.get('ZBX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS', 'zabbix')

SUBS = {
    '__GLPI_APP_TOKEN__': os.environ.get('GLPI_APP_TOKEN', ''),
    '__GLPI_PASSWORD__': os.environ.get('GLPI_API_PASSWORD', ''),
    '__GLPI_USER__': os.environ.get('GLPI_USER', 'glpi'),
}

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
    missing = [k for k, v in SUBS.items() if not v]
    if missing:
        print(f'ERROR: missing env for {missing} — export .env first.')
        sys.exit(1)
    here = os.path.dirname(os.path.abspath(__file__))
    xml = open(os.path.join(here, '..', 'config', 'zabbix',
                            'zbx_export_mediatypes.xml')).read()
    for k, v in SUBS.items():
        xml = xml.replace(k, v)
    if '__GLPI' in xml:
        print('ERROR: unfilled placeholders remain.')
        sys.exit(1)
    auth = call('user.login', {'username': ZBX_USER, 'password': ZBX_PASS})
    result = call('configuration.import', {
        'format': 'xml',
        'rules': {'mediaTypes': {'createMissing': True, 'updateExisting': True}},
        'source': xml,
    }, auth)
    print(f'Media types import: {result}')
    call('user.logout', [], auth)


if __name__ == '__main__':
    main()
