#!/usr/bin/env python3
"""
ATM State Manager
Updates atm_current_state every 30s by reading each ATM's simulator HTTP API
and deriving a NetXMS-style state. In production this would read from the
Zabbix API instead; here it reads the same HTTP-agent endpoints the Zabbix
template polls, so the Grafana state dashboards stay accurate (fixes the
"misinformation" complaint).

Fixes vs the original dev-guide version:
  * uses stdlib urllib (no extra pip dependency in the simulator image)
  * DB_PASS comes from ${DB_PASS} (not hard-coded "zabbix_pass")
  * GATEWAY_IP defaults to 172.17.0.1 to match the Zabbix item URLs
  * GRG-aware OID tree (2.x cash / 3.x card / 8.x net) like the GRG template
"""
import os
import time
import urllib.request
import urllib.error
import psycopg2
from datetime import datetime

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', 'zabbix_pass')
GATEWAY_IP = os.environ.get('GATEWAY_IP', '172.17.0.1')

# Port map for simulated ATMs (must match docker-compose SNMP_PORT values).
# Only simulated ATMs are polled; the rest keep their seeded state.
ATM_PORTS = {
    'ATM-001': 1161, 'ATM-002': 1162, 'ATM-003': 1163,
    'ATM-004': 1164, 'ATM-005': 1165,
    'GRG-001': 1166, 'GRG-002': 1167,
}


def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS)


def _fetch(port, oid, timeout=3):
    url = f"http://{GATEWAY_IP}:{port}/oid/{oid}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return int(r.read().decode().strip())


def determine_state(atm_id, port, vendor):
    """Derive ATM state from simulator OIDs (same logic the GRG/NCR templates poll)."""
    try:
        base = f"http://{GATEWAY_IP}:{port}"
        status = int(urllib.request.urlopen(f"{base}/oid/1.1.0", timeout=3).read())

        if vendor == 'NCR':
            cash1 = _fetch(port, '1.2.0')
            cash2 = _fetch(port, '1.3.0')
            cash_jam = _fetch(port, '1.7.0')
            card = _fetch(port, '2.1.0')
            net = _fetch(port, '7.1.0')
        else:  # GRG
            cash1 = _fetch(port, '2.1.0')
            cash2 = _fetch(port, '2.2.0')
            cash_jam = _fetch(port, '2.5.0')
            card = _fetch(port, '3.1.0')
            net = _fetch(port, '8.1.0')

        if net == 2:
            return 'OFFLINE'
        elif status == 2:
            return 'OUT_OF_SERVICE'
        elif status == 4:
            return 'IN_SUPERVISOR'
        elif cash1 == 0 and cash2 == 0:
            return 'OUT_OF_CASH'
        elif cash_jam == 1 or card == 2:
            return 'HARDWARE_FAULT'
        elif status == 1:
            return 'IN_SERVICE'
        return 'UNKNOWN'
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return 'AGENT_DISCONNECTED'


def update_states():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT atm_id, vendor FROM atm_locations")
    atms = cur.fetchall()

    for atm_id, vendor in atms:
        port = ATM_PORTS.get(atm_id)
        if not port:
            continue  # no simulator for this ATM — keep seeded state

        new_state = determine_state(atm_id, port, vendor or 'NCR')

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
            cur.execute("""
                UPDATE atm_current_state
                SET last_seen = NOW(), updated_at = NOW()
                WHERE atm_id = %s
            """, (atm_id,))

    conn.commit()
    cur.close()
    conn.close()


print("ATM State Manager started — updating every 30 seconds")
print("This fixes the 'misinformation' problem by keeping state always current")

# Wait for DB
for i in range(30):
    try:
        get_db().close()
        print("Connected to PostgreSQL")
        break
    except Exception:
        print(f"Waiting for DB ({i+1}/30)...")
        time.sleep(5)

while True:
    try:
        update_states()
    except Exception as e:
        print(f"State update error: {e}")
    time.sleep(30)
