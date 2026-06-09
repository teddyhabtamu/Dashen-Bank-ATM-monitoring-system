#!/usr/bin/env python3
import os
import time
import random
from datetime import datetime, timedelta

ATM_ID = os.environ.get('ATM_ID', 'ATM-001')
ATM_TERMINAL_ID = os.environ.get('ATM_TERMINAL_ID', 'TID001')
ATM_BRANCH = os.environ.get('ATM_BRANCH', 'Addis Ababa Main Branch')
EJ_LOG_PATH = os.environ.get('EJ_LOG_PATH', '/var/log/atm-ej/ATM-001.log')

CARD_POOL = [
    '************1234', '************5678', '************9012',
    '************3456', '************7890', '************2345',
    '************6789', '************0123', '************4567',
    '************8901',
]

ERROR_CODES = {
    '3A7F': 'CASH_JAM_CASSETTE',
    'B2C1': 'CARD_READ_ERROR',
    'FF01': 'RECEIPT_PAPER_LOW',
    '44AA': 'NETWORK_TIMEOUT',
    '9E3D': 'DISPENSER_ERROR',
}

def write_ej(entry):
    os.makedirs(os.path.dirname(EJ_LOG_PATH), exist_ok=True)
    with open(EJ_LOG_PATH, 'a') as f:
        f.write(entry + '\n')
        f.flush()

def generate_transaction():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    card = random.choice(CARD_POOL)
    txn_types = ['WITHDRAWAL','BALANCE_INQ','MINI_STATEMENT','TRANSFER','CARDLESS_TXN']
    weights = [0.55, 0.25, 0.10, 0.07, 0.03]
    txn_type = random.choices(txn_types, weights=weights)[0]
    status = random.choices(
        ['APPROVED','DECLINED','TIMEOUT','ERROR'],
        weights=[0.85, 0.08, 0.04, 0.03]
    )[0]
    seq = random.randint(100000, 999999)
    if txn_type in ['WITHDRAWAL', 'TRANSFER']:
        amount = random.choice([100,200,500,1000,2000,5000,10000])
        entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | TXN | {txn_type} | "
                f"SEQ={seq} | CARD={card} | AMOUNT={amount:.2f} | "
                f"CURRENCY=ETB | STATUS={status}")
        if status == 'APPROVED':
            entry += f" | AUTH={random.randint(100000,999999)}"
    else:
        entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | TXN | {txn_type} | "
                f"SEQ={seq} | CARD={card} | STATUS={status}")
    return entry

def generate_error():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    code, desc = random.choice(list(ERROR_CODES.items()))
    cassette = random.randint(1, 4)
    return (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | ERROR | {desc} | "
            f"CODE={code} | CASSETTE={cassette}")

def generate_cash_load():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    amount = random.choice([100000, 150000, 200000, 250000])
    operator = f"OP{random.randint(1,10):03d}"
    cassette = random.randint(1, 4)
    return (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | CASH | CASSETTE_LOADED | "
            f"CASSETTE={cassette} | AMOUNT={amount:.2f} | OPERATOR={operator}")

print(f"[{ATM_ID}] EJ Log Generator started - Branch: {ATM_BRANCH}")
print(f"[{ATM_ID}] Writing to: {EJ_LOG_PATH}")

# Generate 24h backfill history
print(f"[{ATM_ID}] Generating 24h backfill...")
base_time = datetime.now() - timedelta(hours=24)
for i in range(500):
    fake_time = base_time + timedelta(minutes=i*2.88)
    ts = fake_time.strftime('%Y-%m-%d %H:%M:%S')
    card = random.choice(CARD_POOL)
    amount = random.choice([100,200,500,1000,2000,5000])
    status = random.choices(
        ['APPROVED','DECLINED','TIMEOUT'],
        weights=[0.85,0.10,0.05]
    )[0]
    entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | TXN | WITHDRAWAL | "
            f"SEQ={random.randint(100000,999999)} | CARD={card} | "
            f"AMOUNT={amount:.2f} | CURRENCY=ETB | STATUS={status}")
    if status == 'APPROVED':
        entry += f" | AUTH={random.randint(100000,999999)}"
    write_ej(entry)

print(f"[{ATM_ID}] Backfill complete. Starting live generation...")

# Live generation loop
while True:
    hour = time.localtime().tm_hour
    if hour in [8, 9, 12, 13, 17, 18]:
        sleep_time = random.uniform(8, 20)
    elif hour in [0, 1, 2, 3, 4]:
        sleep_time = random.uniform(120, 300)
    else:
        sleep_time = random.uniform(30, 60)

    rand = random.random()
    if rand < 0.92:
        entry = generate_transaction()
    elif rand < 0.97:
        entry = generate_error()
    else:
        entry = generate_cash_load()

    write_ej(entry)
    print(f"[{ATM_ID}] {entry[:80]}...")
    time.sleep(sleep_time)
