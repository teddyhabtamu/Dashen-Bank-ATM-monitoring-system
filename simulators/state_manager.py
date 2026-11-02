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
from zabbix_modules.state_freshness import source_freshness
from zabbix_modules.xfs_metrics import XFS_STATE_KEYS, is_xfs, normalize

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

ZABBIX_URL = os.environ.get('ZBX_URL') or os.environ.get('ZABBIX_URL', 'http://zabbix-web:8080/api_jsonrpc.php')
ZABBIX_USER = os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER', 'Admin')
ZABBIX_PASS = os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS', 'zabbix')

POLL_INTERVAL = int(os.environ.get('STATE_POLL_INTERVAL', '15'))
STATE_DEBUG = os.environ.get('STATE_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')

STATE_KEYS = [
    'atm.status', 'atm.cash.cassette1', 'atm.cash.cassette2',
    'atm.cash.jam', 'atm.card.reader', 'atm.net.link',
] + list(XFS_STATE_KEYS)


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
        if res.get('result'):
            return res['result']
        # Bad credentials come back as an error OBJECT, not an exception, so
        # this used to return None with no message at all - the state manager
        # then wrote no Zabbix state and looked merely idle.
        err = res.get('error') or {}
        print(f"[ZABBIX] login rejected: {err.get('data') or err.get('message') or err}")
        return None
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


def _fresh_meta(items):
    """Split Zabbix items into values, clocks and delays.

    Zabbix reports lastvalue "0" for items that never collected — use
    lastclock to tell those apart from genuine zeros, otherwise a dataless
    host reads OUT_OF_CASH instead of UNREACHABLE.
    """
    values, clocks, delays = {}, {}, {}
    for item in items:
        key = item['key_']
        try:
            clock = int(item.get('lastclock') or 0)
        except (TypeError, ValueError):
            clock = 0
        values[key] = item.get('lastvalue') if clock else None
        clocks[key] = clock
        delays[key] = item.get('delay')
    return values, clocks, delays


# Sentinel: the Zabbix API could not answer. Distinct from "no data" and from
# "host absent". Without this, an API hiccup was indistinguishable from a dead
# ATM and every machine in the fleet was written as UNREACHABLE - a monitoring
# tool crying wolf about itself, which is worse than reporting nothing.
FETCH_FAILED = object()

AGENT_INTERFACE = 1
SNMP_INTERFACE = 2


def _interface_availability(interfaces, interface_type):
    """Zabbix's verdict for one interface type (1 up, 2 down, 0 unknown)."""
    for iface in interfaces or ():
        try:
            if int(iface.get('type', -1)) != interface_type:
                continue
        except (TypeError, ValueError):
            continue
        value = iface.get('available')
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _relevant_availability(metrics, interfaces):
    """Pick the transport that actually carries the state, never the first one.

    A host usually has two interfaces: SNMP (type 2) and Zabbix agent (type 1).
    The old code took whichever interface Zabbix happened to return first and
    treated its ``available`` flag as proof the whole ATM was down. On the GRG
    test host the SNMP interface can be unavailable while the XFS agent is
    delivering fresh data, which is exactly how a healthy ATM became
    "Agent Disconnected".

    Returns ``(relevant, agent, snmp)`` so diagnostics can show both verdicts.
    """
    agent = _interface_availability(interfaces, AGENT_INTERFACE)
    snmp = _interface_availability(interfaces, SNMP_INTERFACE)

    if is_xfs(metrics) or (metrics or {}).get('atm.parts') not in (None, ''):
        # GRG XFS and NCR WMI both ride the Zabbix agent.
        chosen = agent if agent is not None else snmp
        return chosen, agent, snmp

    has_snmp_values = any((metrics or {}).get(key) not in (None, '')
                          for key in ('atm.status', 'atm.net.link',
                                      'atm.cash.cassette1'))
    if has_snmp_values:
        chosen = snmp if snmp is not None else agent
        return chosen, agent, snmp

    # No telemetry yet: prefer an interface that is demonstrably up. If both
    # are down, either verdict proves the machine is unreachable.
    if agent == 1 and snmp != 1:
        return agent, agent, snmp
    if snmp == 1 and agent != 1:
        return snmp, agent, snmp
    if agent == 2 and snmp != 2:
        return snmp, agent, snmp
    if snmp == 2 and agent != 2:
        return agent, agent, snmp
    return (agent if agent is not None else snmp), agent, snmp


def fetch_host_metrics(auth, host):
    """Return ``(metrics, availability, source_age, source_allowed)``.

    ``metrics`` is None when the host is not in Zabbix and FETCH_FAILED when
    the API did not answer.  ``availability`` is the verdict for the interface
    that actually carries the state source (agent for XFS/WMI, SNMP for the
    legacy OIDs).  ``source_age``/``source_allowed`` say whether the values are
    still inside their own polling budget; a stale snapshot is not evidence of
    the current state.
    """
    hosts = zbx_call('host.get',
                     {'output': ['hostid'],
                      'selectInterfaces': ['type', 'available', 'main', 'port'],
                      'filter': {'host': [host]}}, auth)
    if hosts is None:
        return FETCH_FAILED, None, None, None
    if not hosts:
        return None, None, None, None
    interfaces = hosts[0].get('interfaces') or []
    hostid = hosts[0]['hostid']
    items = zbx_call('item.get', {'output': ['key_', 'lastvalue', 'lastclock', 'delay'],
                                  'hostids': [hostid],
                                  'filter': {'key_': STATE_KEYS}}, auth)
    if items is None:
        return FETCH_FAILED, None, None, None
    merged, clocks, delays = _fresh_meta(items)
    # WMI (NCR agent) path: master + per-part dependents. `search` is
    # substring-based — 'atm.part.' matches dependents, never 'atm.parts'.
    wmi = zbx_call('item.get', {'output': ['key_', 'lastvalue', 'lastclock', 'delay'],
                                'hostids': [hostid],
                                'filter': {'key_': ['atm.parts']}}, auth) or []
    deps = zbx_call('item.get', {'output': ['key_', 'lastvalue', 'lastclock', 'delay'],
                                 'hostids': [hostid],
                                 'search': {'key_': 'atm.part.'}}, auth) or []
    wmi_values, wmi_clocks, wmi_delays = _fresh_meta(wmi + deps)
    merged.update(wmi_values)
    clocks.update(wmi_clocks)
    delays.update(wmi_delays)
    availability, _agent, _snmp = _relevant_availability(merged, interfaces)
    age, allowed = source_freshness(merged, clocks, delays)
    return normalize(merged), availability, age, allowed


def determine_state(m, vendor='NCR', availability=None, source_current=True):
    """State for a host Zabbix knows about.

    Fresh telemetry wins over the transport flag: ``interface.available``
    records whichever poll most recently succeeded and can lag or describe the
    SNMP interface while the agent is fine.  When the source is not current,
    only a positive "unavailable" verdict is proof of an outage; absent or
    stale telemetry is UNKNOWN, not UNREACHABLE.
    """
    if source_current and m:
        return interpret_state(vendor, m)
    if availability == 2:
        return 'UNREACHABLE'
    if availability == 1:
        return 'UNKNOWN'
    # No positive transport verdict. Do not turn "we have not measured this"
    # into "the machine is down".
    return 'UNKNOWN'


# A state must be seen this many times in a row before it is believed. Two
# readings at the 15s poll settle a real change in ~30s; a single unlucky poll
# is still rejected.
#
# Seen live on UAT 2026-10-17: ATM-TEST-NCR alternated UNREACHABLE ->
# HARDWARE_FAULT -> UNREACHABLE on every 30s poll, for 24 hours. Zabbix's
# interface.available records whichever poll most recently succeeded, so one
# unlucky timeout on a flaky link flips the verdict.
#
# Flapping is not cosmetic. It writes an atm_state_history segment per flip -
# and the availability percentage is computed from those segments, so that
# number becomes meaningless - it churns the dashboard, and once triggers follow
# state changes it pages someone every 30 seconds, which is how alerts get
# ignored. Debouncing belongs here, once, rather than in every trigger.
STATE_CONFIRMATIONS = int(os.environ.get('STATE_CONFIRMATIONS', '2'))
_pending = {}          # atm_id -> [candidate_state, consecutive_count]


def _debounced(atm_id, current_state, candidate):
    """True once `candidate` has been seen STATE_CONFIRMATIONS times running."""
    if candidate == current_state:
        _pending.pop(atm_id, None)
        return True, 0
    held = _pending.get(atm_id)
    if held and held[0] == candidate:
        held[1] += 1
        count = held[1]
    else:
        count = 1
        _pending[atm_id] = [candidate, count]
    if count < STATE_CONFIRMATIONS:
        return False, count
    _pending.pop(atm_id, None)
    return True, count


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
        m, avail, source_age, source_allowed = fetch_host_metrics(auth, atm_id)
        if m is FETCH_FAILED:
            # Our monitoring path is broken, not the ATM. Leave the last known
            # state untouched rather than overwrite a healthy machine with an
            # outage we invented.
            print(f"[WARN] Zabbix did not answer for {atm_id}; "
                  f"leaving its state unchanged")
            continue
        if m is None:
            # Registered here, absent in Zabbix: a provisioning gap, not an outage.
            state = 'NOT_PROVISIONED'
        else:
            source_current = (source_age is not None
                              and source_allowed is not None
                              and source_age <= source_allowed)
            state = determine_state(m, vendor, avail,
                                    source_current=source_current)
            if STATE_DEBUG:
                print(f"[STATE READ] {atm_id}: source {source_age}s/"
                      f"{source_allowed}s current={source_current} "
                      f"transport={avail} candidate={state}")
        results.append((atm_id, vendor or 'NCR', state))

    conn = get_db()
    try:
        cur = conn.cursor()
        # History table doubles as its own migration (older deploys lack it).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS atm_state_history (
                id SERIAL PRIMARY KEY,
                atm_id VARCHAR(20) NOT NULL REFERENCES atm_locations(atm_id) ON DELETE CASCADE,
                state VARCHAR(30) NOT NULL,
                started_at TIMESTAMP DEFAULT NOW(),
                ended_at TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_state_hist_atm "
                    "ON atm_state_history(atm_id, started_at)")
        for atm_id, vendor, new_state in results:
            cur.execute("SELECT state FROM atm_current_state WHERE atm_id = %s", (atm_id,))
            row = cur.fetchone()
            current_state = row[0] if row else None
            settled, seen = _debounced(atm_id, current_state, new_state)
            if not settled:
                # No state write, but last_seen keeps moving: that is how
                # the portal tells "we could not read this machine" apart
                # from "this machine went away".
                cur.execute("UPDATE atm_current_state SET last_seen = NOW(), "
                            "updated_at = NOW() WHERE atm_id = %s", (atm_id,))
                print(f"[STATE HOLD] {atm_id}: {new_state} seen "
                      f"{seen}/{STATE_CONFIRMATIONS}, keeping {current_state}")
                continue
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
                # close the previous history segment, open the new one
                cur.execute("UPDATE atm_state_history SET ended_at = NOW() "
                            "WHERE atm_id = %s AND ended_at IS NULL", (atm_id,))
                cur.execute("INSERT INTO atm_state_history (atm_id, state) "
                            "VALUES (%s, %s)", (atm_id, new_state))
            else:
                cur.execute("UPDATE atm_current_state SET last_seen = NOW(), updated_at = NOW() WHERE atm_id = %s", (atm_id,))
                # backfill: ATMs whose current state predates history tracking
                # get an open segment starting at their last change
                cur.execute("""
                    INSERT INTO atm_state_history (atm_id, state, started_at)
                    SELECT %s, %s, COALESCE(
                        (SELECT state_changed_at FROM atm_current_state WHERE atm_id = %s), NOW())
                    WHERE NOT EXISTS (
                        SELECT 1 FROM atm_state_history WHERE atm_id = %s AND ended_at IS NULL)
                """, (atm_id, new_state, atm_id, atm_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def main():
    """Entry point. Kept out of module scope on purpose.

    The file is run as a script (the container command is
    `python3 /app/state_manager.py`), so this still runs exactly as before.
    But a module-level `while True:` means the file cannot be imported,
    which is why none of the pure logic here - determine_state,
    _debounced, fetch_host_metrics - could ever be unit-tested.
    """
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



if __name__ == "__main__":
    main()