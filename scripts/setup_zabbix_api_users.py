#!/usr/bin/env python3
"""Dedicated Zabbix API service accounts for Dashen Bank ATM Monitoring.

Replaces the shared Admin login (one stale password used to lock out
monitoring, reporting, backups and dashboards together) with three
isolated accounts — one password problem can never again cascade:

  svc-portal   portal sync + monitoring pages + reconcile jobs
  svc-grafana  Grafana Zabbix datasource (dashboards)
  svc-scripts  operator scripts + nightly config backups

Design decisions (deliberate, documented):
- All three join the existing "Zabbix administrators" group with the
  "Admin role" (looked up by name — no hardcoded IDs). Same effective
  rights as today, so cutover cannot break functionality by permission.
  Least-privilege tightening is a separate phase-2 job, not smuggled in.
- Passwords are generated, printed ONCE, and never written anywhere by
  this script. Store them in .env + the sealed laptop backup, then they
  exist in exactly one place each.
- Idempotent: existing accounts are reported and left untouched
  (rotation is an explicit future operation, never a side effect).

Env (run with the current Admin credentials):
    ZBX_URL / ZBX_USER / ZBX_PASS   (defaults: localhost:8080 / Admin)

After creating: follow docs/zabbix-api-users.md cutover (one consumer
at a time, verify each, rollback = put the old value back + recreate).
"""
import os
import secrets
import sys

import requests

ZBX_URL = os.environ.get('ZBX_URL') or os.environ.get(
    'ZABBIX_URL', 'http://localhost:8080/api_jsonrpc.php')
ZBX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZBX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', '')

ACCOUNTS = ('svc-portal', 'svc-grafana', 'svc-scripts')
ADMIN_ROLE_NAME = 'Admin role'
ADMIN_GROUP_NAME = 'Zabbix administrators'

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

    roles = call('role.get', {'filter': {'name': ADMIN_ROLE_NAME},
                              'output': ['roleid', 'name']}, auth)
    if not roles:
        print(f'ERROR: user role "{ADMIN_ROLE_NAME}" not found — aborting.')
        return 1
    roleid = roles[0]['roleid']

    groups = call('usergroup.get', {'filter': {'name': ADMIN_GROUP_NAME},
                                    'output': ['usrgrpid', 'name']}, auth)
    if not groups:
        print(f'ERROR: user group "{ADMIN_GROUP_NAME}" not found — aborting.')
        return 1
    usrgrpid = groups[0]['usrgrpid']

    created = {}
    for username in ACCOUNTS:
        existing = call('user.get', {'filter': {'username': username},
                                     'output': ['userid', 'username']}, auth)
        if existing:
            print(f'{username}: already exists (id {existing[0]["userid"]}) — untouched')
            continue
        password = secrets.token_urlsafe(24)
        res = call('user.create', {
            'username': username,
            'passwd': password,
            'roleid': roleid,
            'usrgrps': [{'usrgrpid': usrgrpid}],
        }, auth)
        created[username] = (res['userids'][0], password)
        print(f'{username}: created (id {res["userids"][0]})')

    call('user.logout', [], auth)

    if created:
        print()
        print('STORE THESE NOW — printed once, never stored by this script:')
        for username, (uid, password) in created.items():
            print(f'  {username} / {password}')
        print()
        print('Next: docs/zabbix-api-users.md cutover (one consumer at a time).')
    else:
        print('Nothing to do — all service accounts exist.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
