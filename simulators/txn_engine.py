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


def insert_one(conn, atm):
    txn_types = ['WITHDRAWAL', 'BALANCE_INQ', 'MINI_STATEMENT', 'TRANSFER', 'CARDLESS_TXN']
    weights = [0.55, 0.25, 0.10, 0.07, 0.03]
    txn_type = random.choices(txn_types, weights=weights)[0]
    status = random.choices(['APPROVED', 'DECLINED', 'TIMEOUT', 'ERROR'],
                             weights=[0.85, 0.08, 0.04, 0.03])[0]
    card = random.choice(CARD_POOL)
    amount = random.choice([100, 200, 500, 1000, 2000, 5000, 10000]) \
        if txn_type in ('WITHDRAWAL', 'TRANSFER') else None
    auth = str(random.randint(100000, 999999)) if status == 'APPROVED' else None
    err = random.choice(['3A7F', 'B2C1', '44AA']) if status == 'ERROR' else None
    fault_type = NCR_FAULT_MAP.get(err) if err else None
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
            hour = time.localtime().tm_hour
            for atm in atms:
                if hour in (8, 9, 12, 13, 17, 18):
                    n = random.randint(1, 3)
                elif hour in (0, 1, 2, 3, 4):
                    n = random.randint(0, 1)
                else:
                    n = random.randint(0, 2)
                for _ in range(n):
                    insert_one(conn, atm)
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
