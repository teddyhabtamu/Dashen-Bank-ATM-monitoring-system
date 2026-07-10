#!/usr/bin/env python3
"""
GRG ATM Electronic Journal Generator
GRG EJ format is different from NCR — different field names and structure.
"""
import os, time, random
from datetime import datetime, timedelta

ATM_ID = os.environ.get('ATM_ID', 'GRG-001')
ATM_TERMINAL_ID = os.environ.get('ATM_TERMINAL_ID', 'TID006')
ATM_BRANCH = os.environ.get('ATM_BRANCH', 'Bole Atlas Branch')
EJ_LOG_PATH = os.environ.get('EJ_LOG_PATH', '/var/log/atm-ej/GRG-001.log')

CARD_POOL = ['************1234', '************5678', '************9012',
             '************3456', '************7890', '************2345']

# GRG uses different fault names
GRG_FAULTS = {
    'CM001': 'CASH_MODULE_ERROR',
    'CU002': 'CARD_UNIT_FAULT',
    'PB003': 'PURGE_BIN_FULL',
    'TP004': 'THERMAL_PRINTER_FAULT',
    'NE005': 'COMM_ERROR',
    'PP006': 'PIN_PAD_ERROR',
    'CJ007': 'CASH_JAM',
}

def write_ej(entry):
    os.makedirs(os.path.dirname(EJ_LOG_PATH), exist_ok=True)
    with open(EJ_LOG_PATH, 'a') as f:
        f.write(entry + '\n')
        f.flush()

def generate_txn():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    card = random.choice(CARD_POOL)
    # GRG uses slightly different field names (ACCT_NO instead of CARD)
    txn_types = ['WITHDRAWAL', 'BALANCE_INQUIRY', 'MINI_STMT', 'FUND_TRANSFER']
    weights = [0.55, 0.25, 0.12, 0.08]
    txn_type = random.choices(txn_types, weights=weights)[0]
    status = random.choices(['APPROVED', 'DECLINED', 'TIMEOUT', 'ERROR'],
                                weights=[0.85, 0.08, 0.04, 0.03])[0]
    seq = random.randint(100000, 999999)
    if txn_type in ['WITHDRAWAL', 'FUND_TRANSFER']:
        amount = random.choice([100, 200, 500, 1000, 2000, 5000, 10000])
        # GRG format: uses ACCT_NO, TXN_CODE, RESP_CODE instead of NCR's CARD, TXN, STATUS
        entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | VENDOR=GRG | "
                f"TXN_CODE={txn_type} | SEQ={seq} | ACCT_NO={card} | "
                f"AMOUNT={amount:.2f} | CURRENCY=ETB | RESP_CODE={status}")
        if status == 'APPROVED':
            entry += f" | AUTH_CODE={random.randint(100000, 999999)}"
    else:
        entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | VENDOR=GRG | "
                f"TXN_CODE={txn_type} | SEQ={seq} | ACCT_NO={card} | RESP_CODE={status}")
    return entry

def generate_fault():
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    code, desc = random.choice(list(GRG_FAULTS.items()))
    # GRG format: uses FAULT_CODE and FAULT_DESC
    return (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | VENDOR=GRG | "
            f"EVENT=FAULT | FAULT_CODE={code} | FAULT_DESC={desc}")

print(f"[{ATM_ID}] GRG EJ Generator started — Writing to: {EJ_LOG_PATH}")

# Backfill 24h history
base_time = datetime.now() - timedelta(hours=24)
for i in range(400):
    fake_time = base_time + timedelta(minutes=i * 3.6)
    ts = fake_time.strftime('%Y-%m-%d %H:%M:%S')
    card = random.choice(CARD_POOL)
    amount = random.choice([100, 200, 500, 1000, 2000])
    status = random.choices(['APPROVED', 'DECLINED'], weights=[0.85, 0.15])[0]
    entry = (f"{ts} | {ATM_ID} | {ATM_TERMINAL_ID} | VENDOR=GRG | "
            f"TXN_CODE=WITHDRAWAL | SEQ={random.randint(100000, 999999)} | "
            f"ACCT_NO={card} | AMOUNT={amount:.2f} | CURRENCY=ETB | RESP_CODE={status}")
    write_ej(entry)

print(f"[{ATM_ID}] Backfill complete. Starting live generation...")

# Live generation loop
while True:
    hour = time.localtime().tm_hour
    sleep_time = random.uniform(8, 20) if hour in [8, 9, 12, 13, 17, 18] else random.uniform(30, 60)
    r = random.random()
    entry = generate_txn() if r < 0.93 else generate_fault()
    write_ej(entry)
    print(f"[{ATM_ID}] {entry[:80]}...")
    time.sleep(sleep_time)
