#!/usr/bin/env python3
import os
import time
import random
import psycopg2
from datetime import datetime

ATM_ID = os.environ.get('ATM_ID', 'ATM-001')
ATM_TERMINAL_ID = os.environ.get('ATM_TERMINAL_ID', 'TID001')
ATM_BRANCH = os.environ.get('ATM_BRANCH', 'Addis Ababa Main Branch')
DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')

CARD_POOL = [
    '************1234', '************5678', '************9012',
    '************3456', '************7890', '************2345',
]

def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS
    )

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
                source VARCHAR(20) DEFAULT 'SIMULATOR'
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_txn_atm_id ON atm_transactions(atm_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_txn_recorded_at ON atm_transactions(recorded_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_txn_card ON atm_transactions(card_masked)")
    conn.commit()
    print(f"[{ATM_ID}] Database table ready")

def insert_transaction(conn):
    txn_types = ['WITHDRAWAL','BALANCE_INQ','MINI_STATEMENT','TRANSFER','CARDLESS_TXN']
    weights = [0.55, 0.25, 0.10, 0.07, 0.03]
    txn_type = random.choices(txn_types, weights=weights)[0]
    status = random.choices(
        ['APPROVED','DECLINED','TIMEOUT','ERROR'],
        weights=[0.85, 0.08, 0.04, 0.03]
    )[0]
    card = random.choice(CARD_POOL)
    amount = random.choice([100,200,500,1000,2000,5000,10000]) if txn_type in ['WITHDRAWAL','TRANSFER'] else None
    auth = str(random.randint(100000,999999)) if status == 'APPROVED' else None
    err = random.choice(['3A7F','B2C1','44AA']) if status == 'ERROR' else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO atm_transactions
            (atm_id, terminal_id, branch, txn_type, card_masked,
             amount, currency, status, auth_code, error_code, seq_number, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ATM_ID, ATM_TERMINAL_ID, ATM_BRANCH, txn_type, card,
              amount, 'ETB', status, auth, err,
              str(random.randint(100000,999999)), 'SIMULATOR'))
    conn.commit()
    return txn_type, status, amount

print(f"[{ATM_ID}] Transaction Feed starting - Branch: {ATM_BRANCH}")

conn = None
for attempt in range(30):
    try:
        conn = get_db()
        init_db(conn)
        print(f"[{ATM_ID}] Connected to PostgreSQL")
        break
    except Exception as e:
        print(f"[{ATM_ID}] Waiting for DB ({attempt+1}/30): {e}")
        time.sleep(5)

if not conn:
    print(f"[{ATM_ID}] Could not connect to DB after 30 attempts. Exiting.")
    exit(1)

print(f"[{ATM_ID}] Generating live transactions...")

while True:
    try:
        hour = time.localtime().tm_hour
        if hour in [8, 9, 12, 13, 17, 18]:
            sleep_time = random.uniform(5, 15)
        elif hour in [0, 1, 2, 3, 4]:
            sleep_time = random.uniform(60, 180)
        else:
            sleep_time = random.uniform(20, 45)
        txn_type, status, amount = insert_transaction(conn)
        print(f"[{ATM_ID}] TXN: {txn_type} | {status} | {'ETB '+str(amount) if amount else 'N/A'}")
        time.sleep(sleep_time)
    except Exception as e:
        print(f"[{ATM_ID}] DB error, reconnecting: {e}")
        time.sleep(10)
        try:
            conn = get_db()
        except:
            pass
