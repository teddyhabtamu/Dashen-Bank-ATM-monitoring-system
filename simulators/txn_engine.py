#!/usr/bin/env python3
"""
Multi-tenant transaction feed (replaces txn-feed-NNN / grg-txn-NNN).

Generates live ATM transactions into `atm_transactions` for EVERY ATM found in
`atm_locations`, so a newly registered ATM automatically starts producing
transactions (visible on the Report Portal detail page and Grafana).
"""
import os
import time
import random
from datetime import datetime

import psycopg2
import common

DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

CARD_POOL = [
    '************1234', '************5678', '************9012',
    '************3456', '************7890', '************2345',
    '************6789', '************0123', '************4567', '************8901',
]

NCR_FAULT_MAP = {
    '3A7F': 'DISPENSER', 'B2C1': 'CARD_READER',
    'FF01': 'RECEIPT_PRINTER', '44AA': 'NETWORK', '9E3D': 'DISPENSER',
}


def get_db():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS atm_transactions (
                id SERIAL PRIMARY KEY,
                recorded_at TIMESTAMP DEFAULT NOW(),
                atm_id VARCHAR(20),
                terminal_id VARCHAR(20),
                branch VARCHAR(100),
                txn_type VARCHAR(30),
                card_masked VARCHAR(20),
                amount DECIMAL(15,2),
                currency VARCHAR(5),
                status VARCHAR(20),
                auth_code VARCHAR(20),
                error_code VARCHAR(10),
                seq_number VARCHAR(20),
                iso_mti VARCHAR(10),
                iso_processing_code VARCHAR(10),
                iso_stan VARCHAR(20),
                source VARCHAR(20) DEFAULT 'SIMULATOR',
                vendor VARCHAR(20)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_txn_atm_id ON atm_transactions(atm_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_txn_recorded_at ON atm_transactions(recorded_at)")
    conn.commit()


# Cash-dispensing transaction types are the ones blocked by a dispenser /
# cash-level fault. Non-cash inquiries can still succeed during a jam.
CASH_TXN_TYPES = {'WITHDRAWAL', 'TRANSFER', 'CARDLESS_TXN'}


def load_states(conn):
    """Bulk-load current ATM states once per cycle (cheap even at 2,500 ATMs).

    Returns {atm_id: state}. ATMs with no row are treated as IN_SERVICE.
    """
    states = {}
    with conn.cursor() as cur:
        cur.execute("SELECT atm_id, state FROM atm_current_state")
        for aid, st in cur.fetchall():
            states[aid] = st
    return states


def _decide_outcome(txn_type, state):
    """Return (status, error_code, fault_type) biased by the ATM's health.

    The logic mirrors reality so the transaction feed stays consistent with
    the ATM's reported state:
      * HARDWARE_FAULT / OUT_OF_CASH -> cash dispensing txns fail with a
        DISPENSER fault; balance/mini-statement still succeed.
      * AGENT_DISCONNECTED / OFFLINE -> nearly everything fails (timeout /
        network); a rare success mimics a flapping link.
      * IN_SERVICE / IN_SUPERVISOR / others -> normal random mix.
    It is a bias, not a hard 100% block, to stay realistic.
    """
    is_cash = txn_type in CASH_TXN_TYPES

    if state in ('HARDWARE_FAULT', 'OUT_OF_CASH'):
        if is_cash:
            # Dispenser cannot hand out cash -> hardware error.
            return 'ERROR', '9E3D', 'DISPENSER'
        return None  # fall through to normal mix for non-cash txns

    if state in ('AGENT_DISCONNECTED', 'OFFLINE'):
        # Link/agent down: mostly fail, rare success.
        if random.random() < 0.92:
            if is_cash or random.random() < 0.5:
                return 'TIMEOUT', '44AA', 'NETWORK'
            return 'ERROR', '44AA', 'NETWORK'
        return None

    return None  # IN_SERVICE etc. -> normal


def insert_one(conn, atm, state=None):
    txn_types = ['WITHDRAWAL', 'BALANCE_INQ', 'MINI_STATEMENT', 'TRANSFER', 'CARDLESS_TXN']
    weights = [0.55, 0.25, 0.10, 0.07, 0.03]
    txn_type = random.choices(txn_types, weights=weights)[0]

    forced = _decide_outcome(txn_type, state)
    if forced:
        status, err, fault_type = forced
    else:
        status = random.choices(['APPROVED', 'DECLINED', 'TIMEOUT', 'ERROR'],
                                 weights=[0.85, 0.08, 0.04, 0.03])[0]
        err = random.choice(['3A7F', 'B2C1', '44AA']) if status == 'ERROR' else None
        fault_type = NCR_FAULT_MAP.get(err) if err else None

    card = random.choice(CARD_POOL)
    amount = random.choice([100, 200, 500, 1000, 2000, 5000, 10000]) \
        if txn_type in ('WITHDRAWAL', 'TRANSFER') else None
    auth = str(random.randint(100000, 999999)) if status == 'APPROVED' else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO atm_transactions
                (atm_id, terminal_id, branch, txn_type, card_masked,
                 amount, currency, status, auth_code, error_code,
                 seq_number, source, vendor, fault_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (atm['atm_id'], atm['terminal_id'], atm['branch'], txn_type, card,
              amount, 'ETB', status, auth, err,
              str(random.randint(100000, 999999)), 'SIMULATOR',
              atm['vendor'], fault_type))
    conn.commit()


def main():
    conn = None
    for attempt in range(30):
        try:
            conn = get_db()
            init_db(conn)
            print("Connected to PostgreSQL")
            break
        except Exception as e:
            print(f"Waiting for DB ({attempt+1}/30): {e}")
            time.sleep(5)
    if not conn:
        print("Could not connect to DB. Exiting.")
        return

    print("Multi-tenant Transaction Feed started — generating live transactions")
    while True:
        try:
            atms = common.refresh(conn)
            # Bulk-load current states once per cycle so transactions stay
            # consistent with each ATM's reported health.
            states = load_states(conn)
            hour = time.localtime().tm_hour
            for atm in atms:
                if hour in (8, 9, 12, 13, 17, 18):
                    n = random.randint(1, 3)
                elif hour in (0, 1, 2, 3, 4):
                    n = random.randint(0, 1)
                else:
                    n = random.randint(0, 2)
                atm_state = states.get(atm['atm_id'])
                for _ in range(n):
                    insert_one(conn, atm, atm_state)
            if hour in (8, 9, 12, 13, 17, 18):
                sleep_time = random.uniform(5, 15)
            elif hour in (0, 1, 2, 3, 4):
                sleep_time = random.uniform(60, 180)
            else:
                sleep_time = random.uniform(20, 45)
            time.sleep(sleep_time)
        except Exception as e:
            print(f"TXN error, reconnecting: {e}")
            time.sleep(10)
            try:
                conn = get_db()
            except Exception:
                pass


if __name__ == '__main__':
    main()
