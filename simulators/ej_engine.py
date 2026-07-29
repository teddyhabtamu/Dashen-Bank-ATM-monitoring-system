#!/usr/bin/env python3
"""
Multi-tenant EJ log generator (replaces atm-ej-NNN / grg-ej-NNN).

Writes Electronic Journal logs for EVERY ATM in `atm_locations` to
/var/log/atm-ej/<atm_id>.log (NCR and GRG use their respective formats), so a
newly registered ATM automatically gets EJ history for the EJ Search feature.
"""
import os
import time
import random
import threading
from datetime import datetime, timedelta

import common

EJ_DIR = os.environ.get('EJ_LOG_DIR', '/var/log/atm-ej')

CARD_POOL = [
    '************1234', '************5678', '************9012',
    '************3456', '************7890', '************2345',
    '************6789', '************0123', '************4567', '************8901',
]

NCR_ERROR_CODES = {
    '3A7F': 'CASH_JAM_CASSETTE', 'B2C1': 'CARD_READ_ERROR',
    'FF01': 'RECEIPT_PAPER_LOW', '44AA': 'NETWORK_TIMEOUT', '9E3D': 'DISPENSER_ERROR',
}
GRG_FAULTS = {
    'CM001': 'CASH_MODULE_ERROR', 'CU002': 'CARD_UNIT_FAULT',
    'PB003': 'PURGE_BIN_FULL', 'TP004': 'THERMAL_PRINTER_FAULT',
    'NE005': 'COMM_ERROR', 'PP006': 'PIN_PAD_ERROR', 'CJ007': 'CASH_JAM',
}


def _write(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a') as f:
        f.write(entry + '\n')


def _ncr_txn(ts, atm_id, tid):
    card = random.choice(CARD_POOL)
    txn_type = random.choices(['WITHDRAWAL', 'BALANCE_INQ', 'MINI_STATEMENT', 'TRANSFER', 'CARDLESS_TXN'],
                              weights=[0.55, 0.25, 0.10, 0.07, 0.03])[0]
    status = random.choices(['APPROVED', 'DECLINED', 'TIMEOUT', 'ERROR'],
                             weights=[0.85, 0.08, 0.04, 0.03])[0]
    seq = random.randint(100000, 999999)
    if txn_type in ('WITHDRAWAL', 'TRANSFER'):
        amount = random.choice([100, 200, 500, 1000, 2000, 5000, 10000])
        e = f"{ts} | {atm_id} | {tid} | TXN | {txn_type} | SEQ={seq} | CARD={card} | AMOUNT={amount:.2f} | CURRENCY=ETB | STATUS={status}"
        if status == 'APPROVED':
            e += f" | AUTH={random.randint(100000,999999)}"
    else:
        e = f"{ts} | {atm_id} | {tid} | TXN | {txn_type} | SEQ={seq} | CARD={card} | STATUS={status}"
    return e


def _grg_txn(ts, atm_id, tid):
    card = random.choice(CARD_POOL)
    txn_type = random.choices(['WITHDRAWAL', 'BALANCE_INQUIRY', 'MINI_STMT', 'FUND_TRANSFER'],
                              weights=[0.55, 0.25, 0.12, 0.08])[0]
    status = random.choices(['APPROVED', 'DECLINED', 'TIMEOUT', 'ERROR'],
                             weights=[0.85, 0.08, 0.04, 0.03])[0]
    seq = random.randint(100000, 999999)
    if txn_type in ('WITHDRAWAL', 'FUND_TRANSFER'):
        amount = random.choice([100, 200, 500, 1000, 2000, 5000, 10000])
        e = (f"{ts} | {atm_id} | {tid} | VENDOR=GRG | TXN_CODE={txn_type} | SEQ={seq} | "
             f"ACCT_NO={card} | AMOUNT={amount:.2f} | CURRENCY=ETB | RESP_CODE={status}")
        if status == 'APPROVED':
            e += f" | AUTH_CODE={random.randint(100000,999999)}"
    else:
        e = (f"{ts} | {atm_id} | {tid} | VENDOR=GRG | TXN_CODE={txn_type} | SEQ={seq} | "
             f"ACCT_NO={card} | RESP_CODE={status}")
    return e


def run_atm(atm):
    path = os.path.join(EJ_DIR, f"{atm['atm_id']}.log")
    vendor = atm['vendor']
    tid = atm['terminal_id']

    # 24h backfill
    base = datetime.now() - timedelta(hours=24)
    steps = 500 if vendor != 'GRG' else 400
    for i in range(steps):
        fake = base + timedelta(minutes=i * (24 * 60 / steps))
        ts = fake.strftime('%Y-%m-%d %H:%M:%S')
        if vendor == 'GRG':
            amount = random.choice([100, 200, 500, 1000, 2000])
            st = random.choices(['APPROVED', 'DECLINED'], weights=[0.85, 0.15])[0]
            _write(path, f"{ts} | {atm['atm_id']} | {tid} | VENDOR=GRG | TXN_CODE=WITHDRAWAL | "
                         f"SEQ={random.randint(100000,999999)} | ACCT_NO={random.choice(CARD_POOL)} | "
                         f"AMOUNT={amount:.2f} | CURRENCY=ETB | RESP_CODE={st}")
        else:
            _write(path, _ncr_txn(ts, atm['atm_id'], tid))

    print(f"[EJ] {atm['atm_id']} backfill complete, writing live EJ to {path}")

    while True:
        try:
            hour = time.localtime().tm_hour
            if hour in (8, 9, 12, 13, 17, 18):
                sleep_time = random.uniform(5, 15)
            elif hour in (0, 1, 2, 3, 4):
                sleep_time = random.uniform(60, 180)
            else:
                sleep_time = random.uniform(20, 45)
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if vendor == 'GRG' and random.random() < 0.05:
                code, desc = random.choice(list(GRG_FAULTS.items()))
                _write(path, f"{ts} | {atm['atm_id']} | {tid} | VENDOR=GRG | EVENT=FAULT | "
                             f"FAULT_CODE={code} | FAULT_DESC={desc}")
            else:
                _write(path, _grg_txn(ts, atm['atm_id'], tid) if vendor == 'GRG'
                       else _ncr_txn(ts, atm['atm_id'], tid))
            time.sleep(sleep_time)
        except Exception as e:
            print(f"[EJ] {atm['atm_id']} error: {e}")
            time.sleep(10)


def main():
    registry = {}
    while True:
        try:
            conn = common.get_db()
            atms = common.refresh(conn)
            conn.close()
            active_ids = {atm['atm_id'] for atm in atms}

            # Remove threads for ATMs that are no longer active
            for aid in list(registry.keys()):
                if aid not in active_ids:
                    print(f"[EJ] Removing inactive ATM {aid} from registry")
                    del registry[aid]

            for atm in atms:
                if atm['atm_id'] not in registry:
                    t = threading.Thread(target=run_atm, args=(atm,), daemon=True)
                    t.start()
                    registry[atm['atm_id']] = t
        except Exception as e:
            print(f"[EJ] sync error: {e}")
        time.sleep(10)


if __name__ == '__main__':
    main()
