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
  * DB_PASS comes from ${DB_PASS} (empty fallback fails safely)
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
DB_PASS = os.environ.get('DB_PASS', '')
GATEWAY_IP = os.environ.get('GATEWAY_IP', '172.17.0.1')

# Ports are now read from atm_locations.sim_port (assigned automatically by the
# multi-tenant sim engine). No hard-coded map needed — every ATM is polled.


def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS)


def _fetch(port, oid, timeout=8, retries=2):
    """Fetch an OID value with a retry. A single slow-but-alive simulator
    should not be reported as AGENT_DISCONNECTED."""
    last = None
    for _ in range(retries + 1):
        try:
            url = f"http://{GATEWAY_IP}:{port}/oid/{oid}"
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return int(r.read().decode().strip())
        except Exception as e:
            last = e
            time.sleep(0.3)
    if last is not None:
        raise last
    raise ValueError("no response")


def determine_state(atm_id, port, vendor):
    """Derive ATM state from simulator OIDs.

    Dashen Bank only deploys NCR and GRG ATMs. The simulator models every
    non-GRG vendor as NCR, so we use the same rule — GRG -> GRG OIDs,
    everything else -> NCR OIDs.
    """
    try:
        status = _fetch(port, '1.1.0')   # reachability gate
    except Exception:
        return 'AGENT_DISCONNECTED'

    def metric(oid, default=0):
        try:
            return _fetch(port, oid)
        except Exception:
            return default   # one missing OID should not disconnect the whole ATM

    if vendor == 'GRG':
        cash1 = metric('2.1.0'); cash2 = metric('2.2.0'); cash_jam = metric('2.5.0')
        card = metric('3.1.0'); net = metric('8.1.0')
    else:  # NCR and all other vendors share the NCR OID schema
        cash1 = metric('1.2.0'); cash2 = metric('1.3.0'); cash_jam = metric('1.7.0')
        card = metric('2.1.0'); net = metric('7.1.0')

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


def update_states():
    import common
    conn = get_db()
    cur = conn.cursor()

    # Build the live port map from the DB (every ATM with a sim_port).
    cur.execute("SELECT atm_id, sim_port, vendor FROM atm_locations WHERE sim_port IS NOT NULL")
    port_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    cur.execute("SELECT atm_id, vendor FROM atm_locations")
    atms = cur.fetchall()

    for atm_id, vendor in atms:
        pm = port_map.get(atm_id)
        if not pm:
            continue  # no simulator for this ATM — keep seeded state
        port, vendor = pm

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
