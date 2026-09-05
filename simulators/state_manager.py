#!/usr/bin/env python3
import os
import time
import json
import psycopg2
import urllib.request
import urllib.error
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from zabbix_modules.status_rules import interpret_state

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

ZABBIX_URL = os.environ.get('ZBX_URL') or os.environ.get('ZABBIX_URL', 'http://zabbix-web:8080/api_jsonrpc.php')
ZABBIX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZABBIX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', 'zabbix')

POLL_INTERVAL = int(os.environ.get('STATE_POLL_INTERVAL', '30'))

STATE_KEYS = [
    'atm.status', 'atm.cash.cassette1', 'atm.cash.cassette2',
    'atm.cash.jam', 'atm.card.reader', 'atm.net.link',
]


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def zbx_post(payload):
    req = urllib.request.Request(
        ZABBIX_URL,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json-rpc'},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def zbx_login():
    try:
        res = zbx_post({'jsonrpc': '2.0', 'method': 'user.login',
                        'params': {'username': ZABBIX_USER, 'password': ZABBIX_PASS}, 'id': 1})
        return res.get('result')
    except Exception as e:
        print(f"[ZABBIX] login failed: {e}")
        return None


def zbx_call(method, params, auth):
    try:
        res = zbx_post({'jsonrpc': '2.0', 'method': method, 'params': params, 'auth': auth, 'id': 1})
        return res.get('result')
    except Exception as e:
        print(f"[ZABBIX] call {method} failed: {e}")
        return None


def fetch_host_metrics(auth, host):
    hosts = zbx_call('host.get', {'output': ['hostid'], 'filter': {'host': [host]}}, auth)
    if not hosts:
        return None
    hostid = hosts[0]['hostid']
    items = zbx_call('item.get', {'output': ['key_', 'lastvalue', 'lastclock'], 'hostids': [hostid],
                                  'filter': {'key_': STATE_KEYS}}, auth)
    if not items:
        return {}
    # Zabbix reports lastvalue "0" for items that never collected — use
    # lastclock to tell those apart from genuine zeros, otherwise a dataless
    # host reads OUT_OF_CASH instead of UNREACHABLE.
    return {it['key_']: (it.get('lastvalue') if str(it.get('lastclock')) != '0' else None)
            for it in items}


def determine_state(m, vendor='NCR'):
    return interpret_state(vendor, m)


def update_states(auth):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT atm_id, vendor FROM atm_locations WHERE status = 'active'")
        atms = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    results = []
    for atm_id, vendor in atms:
        m = fetch_host_metrics(auth, atm_id)
        state = determine_state(m, vendor) if m is not None else 'UNREACHABLE'
        results.append((atm_id, vendor or 'NCR', state))

    conn = get_db()
    try:
        cur = conn.cursor()
        for atm_id, vendor, new_state in results:
            cur.execute("SELECT state FROM atm_current_state WHERE atm_id = %s", (atm_id,))
            row = cur.fetchone()
            current_state = row[0] if row else None
            if current_state != new_state:
                print(f"[STATE CHANGE] {atm_id}: {current_state} -> {new_state}")
                cur.execute("""
                    INSERT INTO atm_current_state
                        (atm_id, state, previous_state, state_changed_at, last_seen, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW(), NOW())
                    ON CONFLICT (atm_id) DO UPDATE SET
                        previous_state = atm_current_state.state,
                        state = EXCLUDED.state,
                        state_changed_at = NOW(),
                        last_seen = NOW(),
                        updated_at = NOW()
                """, (atm_id, new_state, current_state))
            else:
                cur.execute("UPDATE atm_current_state SET last_seen = NOW(), updated_at = NOW() WHERE atm_id = %s", (atm_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


print("ATM State Manager (Zabbix-backed) started — updating every %d seconds" % POLL_INTERVAL)

for i in range(30):
    try:
        get_db().close()
        print("Connected to PostgreSQL")
        break
    except Exception:
        print("Waiting for DB (%d/30)..." % (i + 1))
        time.sleep(5)

while True:
    try:
        auth = zbx_login()
        if auth:
            update_states(auth)
        else:
            print("[ZABBIX] login failed — skipping state update this cycle")
    except Exception as e:
        print(f"State update error: {e}")
    time.sleep(POLL_INTERVAL)
