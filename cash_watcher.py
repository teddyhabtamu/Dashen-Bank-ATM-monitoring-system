#!/usr/bin/env python3
"""
Dashen Bank ATM — Automatic cash-load detection (SYSTEM_REVIEW C11 follow-up).

SNMP tells us cassette LEVELS, not load EVENTS: a sensor cannot attest who
loaded cash, when, or how much. The manual Cash Loads form covers the
exception path (loads during monitoring outages); this watcher covers the
common case — a CIT restock shows up as a sharp upward jump in a cassette
count between two polls, and each such jump becomes a cash_replenishments
row with source='AUTO' (recorded_by='system').

Rules (deliberately conservative — a ledger must not invent money):
  * Only single-poll jumps of >= MIN_AUTO_LOAD notes (default 200) count.
    Gradual rises spread over several polls are ignored; CIT loads land
    in one visit while polls are minutes apart.
  * First sighting of a cassette only seeds the baseline (no row) — this
    also prevents a backfill flood on first deploy.
  * Drops (dispensing) only move the baseline down, never create rows.
  * NCR watches cassettes 1-4, GRG modules 1-3 (same keys the portal uses:
    atm.cash.cassetteN).

Runs every CASH_WATCH_INTERVAL seconds (default 300).
"""
import os
import time
import json
import urllib.request
import psycopg2
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [CASH-WATCH] %(levelname)s %(message)s')
log = logging.getLogger(__name__)

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

ZBX_URL = (os.environ.get('ZBX_URL') or os.environ.get('ZABBIX_URL')
           or 'http://zabbix-web:8080/api_jsonrpc.php')
ZBX_USER = (os.environ.get('ZBX_USER') or os.environ.get('ZABBIX_USER') or 'Admin')
ZBX_PASS = (os.environ.get('ZBX_PASS') or os.environ.get('ZABBIX_PASS') or 'zabbix')

INTERVAL = int(os.environ.get('CASH_WATCH_INTERVAL', '300'))
MIN_JUMP = int(os.environ.get('MIN_AUTO_LOAD', '200'))

NCR_KEYS = [f'atm.cash.cassette{i}' for i in (1, 2, 3, 4)]
GRG_KEYS = [f'atm.cash.cassette{i}' for i in (1, 2, 3)]


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS,
                            connect_timeout=10)


def zbx_post(payload):
    req = urllib.request.Request(
        ZBX_URL, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json-rpc'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def zbx_login():
    try:
        return zbx_post({'jsonrpc': '2.0', 'method': 'user.login',
                         'params': {'username': ZBX_USER,
                                    'password': ZBX_PASS}, 'id': 1}).get('result')
    except Exception as e:
        log.warning('Zabbix login failed: %s', e)
        return None


def fetch_levels(auth, host, keys):
    """Return {cassette_n: int_level} for keys with numeric lastvalues."""
    try:
        hosts = zbx_post({'jsonrpc': '2.0', 'method': 'host.get', 'auth': auth,
                          'params': {'output': ['hostid'],
                                     'filter': {'host': [host]}}, 'id': 1})
        if not hosts:
            return {}
        hostid = hosts[0]['hostid']
        items = zbx_post({'jsonrpc': '2.0', 'method': 'item.get', 'auth': auth,
                          'params': {'output': ['key_', 'lastvalue'],
                                     'hostids': [hostid],
                                     'filter': {'key_': keys}}, 'id': 1})
    except Exception as e:
        log.warning('fetch %s failed: %s', host, e)
        return {}
    out = {}
    for it in items or []:
        try:
            n = int(it['key_'].rsplit('cassette', 1)[1])
            out[n] = int(float(it.get('lastvalue')))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def detect(prev, cur, min_jump=MIN_JUMP):
    """Pure jump rule (unit-testable): {cassette: delta} for qualifying loads."""
    hits = {}
    for cassette, level in cur.items():
        if cassette in prev:
            delta = level - prev[cassette]
            if delta >= min_jump:
                hits[cassette] = delta
    return hits


def watch_once(conn, auth):
    """One poll cycle. Returns (loads_recorded, atms_checked)."""
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS atm_cassette_levels ("
                "atm_id VARCHAR(20) NOT NULL, cassette INT NOT NULL, "
                "level INT NOT NULL DEFAULT 0, updated_at TIMESTAMP DEFAULT NOW(), "
                "PRIMARY KEY (atm_id, cassette))")
    cur.execute("SELECT atm_id, vendor FROM atm_locations WHERE status = 'active'")
    atms = cur.fetchall()
    recorded, checked = 0, 0
    for atm_id, vendor in atms:
        keys = GRG_KEYS if (vendor or '').upper() == 'GRG' else NCR_KEYS
        levels = fetch_levels(auth, atm_id, keys)
        if not levels:
            continue
        checked += 1
        cur.execute("SELECT cassette, level FROM atm_cassette_levels WHERE atm_id = %s",
                    (atm_id,))
        prev = {r[0]: r[1] for r in cur.fetchall()}
        for cassette, delta in detect(prev, levels).items():
            cur.execute(
                "INSERT INTO cash_replenishments "
                "(atm_id, cassette, notes_added, recorded_by, notes, source) "
                "VALUES (%s, %s, %s, 'system', %s, 'AUTO')",
                (atm_id, cassette, delta,
                 f'AUTO: +{delta} notes detected ({prev[cassette]} -> {levels[cassette]})'))
            recorded += 1
            log.warning('AUTO load: +%d notes -> %s cassette %d', delta, atm_id, cassette)
        for cassette, level in levels.items():
            cur.execute(
                "INSERT INTO atm_cassette_levels (atm_id, cassette, level, updated_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (atm_id, cassette) DO UPDATE SET level = EXCLUDED.level, "
                "updated_at = NOW()",
                (atm_id, cassette, level))
    conn.commit()
    cur.close()
    return recorded, checked


def main():
    for _ in range(30):
        try:
            get_db().close()
            log.info('Connected to PostgreSQL')
            break
        except Exception:
            log.info('Waiting for DB...')
            time.sleep(5)
    log.info('Cash watcher started (interval=%ds, min_jump=%d)', INTERVAL, MIN_JUMP)
    while True:
        try:
            auth = zbx_login()
            if not auth:
                log.warning('Zabbix login failed — skipping this cycle')
            else:
                conn = get_db()
                try:
                    recorded, checked = watch_once(conn, auth)
                    if recorded:
                        log.info('cycle: %d AUTO load(s) across %d ATM(s)', recorded, checked)
                finally:
                    conn.close()
        except Exception as e:
            log.error('cycle failed: %s', e)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
