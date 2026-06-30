#!/usr/bin/env python3
"""
ISO 8583 Gateway — Apache Camel Equivalent
Integration-ready transaction parser for Dashen Bank ATM Switch.

Current mode: Simulation (generates ISO 8583-like messages)
Production mode: Change the SOURCE to read from real ATM switch TCP socket

To connect real ATM switch:
1. Change mode = 'tcp' in config
2. Set SWITCH_HOST and SWITCH_PORT to real switch IP/port
3. Stop txn-feed containers
4. This container takes over writing to atm_transactions

The database schema and all downstream tools (Grafana, Report Portal)
require ZERO changes when switching from simulation to production.
"""

import os
import json
import time
import random
import socket
import struct
import threading
import psycopg2
from datetime import datetime

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
MODE = os.environ.get('MODE', 'simulation')
SWITCH_HOST = os.environ.get('SWITCH_HOST', '0.0.0.0')
SWITCH_PORT = int(os.environ.get('SWITCH_PORT', '9876'))
DB_HOST = os.environ.get('DB_HOST', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'zabbix')
DB_USER = os.environ.get('DB_USER', 'zabbix')
DB_PASS = os.environ.get('DB_PASS', '')
INTERVAL = int(os.environ.get('INTERVAL', '10'))

# ─── ISO 8583 MESSAGE TYPES ───────────────────────────────────────────────────
MTI_MAP = {
    '0200': 'Financial Request',
    '0210': 'Financial Response',
    '0400': 'Reversal Request',
    '0420': 'Reversal Advice',
    '0800': 'Network Management',
}

PROCESSING_CODES = {
    '012000': 'WITHDRAWAL',
    '310000': 'BALANCE_INQ',
    '400000': 'TRANSFER',
    '050000': 'MINI_STATEMENT',
    '280000': 'CARDLESS_TXN',
}

RESPONSE_CODES = {
    '00': ('APPROVED', None),
    '51': ('DECLINED', None),
    '05': ('DECLINED', None),
    '54': ('DECLINED', None),
    '91': ('TIMEOUT', '44AA'),
    '96': ('ERROR', '3A7F'),
    'B2': ('ERROR', 'B2C1'),
}

ATMS = [
    {'id': 'ATM-001', 'tid': 'TID001',
     'branch': 'Addis Ababa Main Branch'},
    {'id': 'ATM-002', 'tid': 'TID002',
     'branch': 'Bole International Branch'},
    {'id': 'ATM-003', 'tid': 'TID003',
     'branch': 'Merkato Branch'},
    {'id': 'ATM-004', 'tid': 'TID004',
     'branch': 'Hawassa Branch'},
    {'id': 'ATM-005', 'tid': 'TID005',
     'branch': 'Dire Dawa Branch'},
]

CARDS = [
    '4000000000001234',
    '4000000000005678',
    '5200000000009012',
    '4000000000003456',
    '5200000000007890',
]

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS
    )

def store_transaction(conn, txn):
    """
    Write parsed ISO 8583 transaction to PostgreSQL.
    This function is identical whether data comes from
    simulation or real ATM switch — the schema is the same.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO atm_transactions
            (atm_id, terminal_id, branch,
             txn_type, card_masked, amount,
             currency, status, auth_code,
             error_code, seq_number,
             iso_mti, iso_processing_code,
             iso_stan, source)
            VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,
             %s,%s,%s,%s,%s,%s)
        """, (
            txn['atm_id'],
            txn['terminal_id'],
            txn['branch'],
            txn['txn_type'],
            txn['card_masked'],
            txn.get('amount'),
            txn.get('currency', 'ETB'),
            txn['status'],
            txn.get('auth_code'),
            txn.get('error_code'),
            txn.get('stan'),
            txn.get('mti'),
            txn.get('processing_code'),
            txn.get('stan'),
            txn.get('source', 'ISO8583')
        ))
    conn.commit()

# ─── ISO 8583 PARSER ──────────────────────────────────────────────────────────
def parse_iso8583_message(raw_data):
    """
    Parse real ISO 8583 binary message from ATM switch.
    This is what runs in production with real ATMs.

    Real ISO 8583 format:
    - Bytes 0-1:  Message Length (2 bytes, big-endian)
    - Bytes 2-3:  MTI (Message Type Indicator)
    - Bytes 4-11: Primary Bitmap (8 bytes)
    - Remaining:  Data Elements

    In simulation mode this function receives
    pre-parsed dict instead of raw bytes.
    """
    if isinstance(raw_data, dict):
        return raw_data  # Already parsed (simulation)

    try:
        # Parse MTI
        mti = raw_data[2:6].decode('ascii')

        # Parse bitmap (indicates which fields present)
        bitmap_bytes = raw_data[6:14]
        bitmap = int.from_bytes(
            bitmap_bytes, 'big')

        fields = {}
        pos = 14
        field_num = 1

        # Parse data elements based on bitmap
        while field_num <= 64:
            if bitmap & (1 << (64 - field_num)):
                if field_num == 2:
                    # PAN (card number)
                    length = int(
                        raw_data[pos:pos+2])
                    pos += 2
                    pan = raw_data[
                        pos:pos+length
                    ].decode('ascii')
                    fields[2] = pan
                    pos += length
                elif field_num == 3:
                    # Processing code
                    fields[3] = raw_data[
                        pos:pos+6
                    ].decode('ascii')
                    pos += 6
                elif field_num == 4:
                    # Amount
                    fields[4] = int(
                        raw_data[
                            pos:pos+12
                        ].decode('ascii')
                    ) / 100
                    pos += 12
                elif field_num == 11:
                    # STAN
                    fields[11] = raw_data[
                        pos:pos+6
                    ].decode('ascii')
                    pos += 6
                elif field_num == 38:
                    # Auth code
                    fields[38] = raw_data[
                        pos:pos+6
                    ].decode('ascii')
                    pos += 6
                elif field_num == 39:
                    # Response code
                    fields[39] = raw_data[
                        pos:pos+2
                    ].decode('ascii')
                    pos += 2
                elif field_num == 41:
                    # Terminal ID
                    fields[41] = raw_data[
                        pos:pos+8
                    ].decode('ascii').strip()
                    pos += 8

            field_num += 1

        # Map to internal format
        pan = fields.get(2, '')
        masked = (pan[:4] + '************'
                  + pan[-4:] if pan else '')
        pc = fields.get(3, '012000')
        rc = fields.get(39, '00')
        status, err = RESPONSE_CODES.get(
            rc, ('UNKNOWN', None))

        return {
            'mti': mti,
            'processing_code': pc,
            'txn_type': PROCESSING_CODES.get(
                pc, 'UNKNOWN'),
            'card_masked': masked,
            'amount': fields.get(4),
            'stan': fields.get(11, '000000'),
            'auth_code': fields.get(38),
            'status': status,
            'error_code': err,
            'source': 'ISO8583_REAL'
        }

    except Exception as e:
        print(f"Parse error: {e}")
        return None

# ─── SIMULATION MODE ──────────────────────────────────────────────────────────
def generate_simulated_iso8583():
    """
    Generate realistic ISO 8583 transaction.
    Used until real ATM switch is connected.
    Replace this entire function with real
    TCP socket reader for production.
    """
    atm = random.choice(ATMS)
    pc_key = random.choice(
        list(PROCESSING_CODES.keys()))
    pc_val = PROCESSING_CODES[pc_key]

    # Weighted response codes
    rc_weights = (
        ['00'] * 85 +
        ['51'] * 6 +
        ['05'] * 3 +
        ['54'] * 2 +
        ['91'] * 2 +
        ['96'] * 1 +
        ['B2'] * 1
    )
    rc = random.choice(rc_weights)
    status, err_code = RESPONSE_CODES.get(
        rc, ('UNKNOWN', None))

    pan = random.choice(CARDS)
    masked = pan[:4] + '************' + pan[-4:]

    amount = None
    if pc_val in ['WITHDRAWAL', 'TRANSFER']:
        amount = random.choice(
            [100,200,500,1000,2000,5000,10000])

    return {
        'atm_id': atm['id'],
        'terminal_id': atm['tid'],
        'branch': atm['branch'],
        'mti': random.choice(
            ['0200', '0210']),
        'processing_code': pc_key,
        'txn_type': pc_val,
        'card_masked': masked,
        'amount': amount,
        'currency': 'ETB',
        'stan': f'{random.randint(0,999999):06d}',
        'auth_code': (
            f'{random.randint(0,999999):06d}'
            if status == 'APPROVED' else None),
        'status': status,
        'error_code': err_code,
        'source': 'ISO8583_SIM'
    }

# ─── TCP SERVER (Production mode) ─────────────────────────────────────────────
def handle_switch_connection(conn_sock, addr,
                              db_conn):
    """
    Handle real ATM switch TCP connection.
    Activated when MODE=tcp in environment.
    """
    print(f"Switch connected: {addr}")
    try:
        while True:
            # Read message length (2 bytes)
            length_bytes = conn_sock.recv(2)
            if not length_bytes:
                break
            msg_len = struct.unpack(
                '>H', length_bytes)[0]

            # Read full message
            raw_msg = b''
            while len(raw_msg) < msg_len:
                chunk = conn_sock.recv(
                    msg_len - len(raw_msg))
                if not chunk:
                    break
                raw_msg += chunk

            # Parse and store
            txn = parse_iso8583_message(raw_msg)
            if txn:
                store_transaction(db_conn, txn)
                print(f"[REAL] {txn['atm_id']} "
                      f"{txn['txn_type']} "
                      f"{txn['status']}")

    except Exception as e:
        print(f"Switch connection error: {e}")
    finally:
        conn_sock.close()
        print(f"Switch disconnected: {addr}")

def start_tcp_server(db_conn):
    """
    TCP server for real ATM switch.
    Listens for ISO 8583 connections.
    """
    server = socket.socket(
        socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR, 1)
    server.bind((SWITCH_HOST, SWITCH_PORT))
    server.listen(10)
    print(f"ISO 8583 TCP server listening on "
          f"{SWITCH_HOST}:{SWITCH_PORT}")
    print("Waiting for ATM switch connection...")

    while True:
        client, addr = server.accept()
        t = threading.Thread(
            target=handle_switch_connection,
            args=(client, addr, db_conn),
            daemon=True
        )
        t.start()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
print("=" * 50)
print("DASHEN BANK ISO 8583 GATEWAY")
print(f"Mode: {MODE.upper()}")
print(f"DB: {DB_HOST}/{DB_NAME}")
if MODE == 'tcp':
    print(f"Switch port: {SWITCH_PORT}")
print("=" * 50)

# Connect to database with retry
conn = None
for attempt in range(30):
    try:
        conn = get_db()
        print(f"Connected to PostgreSQL")
        break
    except Exception as e:
        print(f"Waiting for DB "
              f"({attempt+1}/30): {e}")
        time.sleep(5)

if not conn:
    print("Cannot connect to database. Exiting.")
    exit(1)

print(f"\nISO 8583 Gateway ready")
print(f"Writing to: atm_transactions table")
print(f"Source tag: ISO8583_SIM "
      f"(change to ISO8583_REAL "
      f"when switch connected)")

if MODE == 'tcp':
    # Production: listen for real switch
    start_tcp_server(conn)
else:
    # Simulation: generate test messages
    print(f"\nSimulation mode: generating "
          f"1 transaction every "
          f"{INTERVAL} seconds")
    print("To switch to production mode:")
    print("  Set MODE=tcp in environment")
    print("  Set SWITCH_HOST to ATM switch IP")
    print("")

    txn_count = 0
    while True:
        try:
            txn = generate_simulated_iso8583()
            store_transaction(conn, txn)
            txn_count += 1
            print(f"[{txn['atm_id']}] "
                  f"ISO8583 "
                  f"{txn['txn_type']:15s} "
                  f"{txn['status']:10s} "
                  f"{'ETB '+str(txn['amount']) if txn['amount'] else 'N/A':15s} "
                  f"(total: {txn_count})")
            time.sleep(INTERVAL)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)
            try:
                conn = get_db()
            except:
                pass
