#!/usr/bin/env python3
"""
ATM Hardware Metrics Simulator - HTTP API
Exposes ATM hardware OIDs via simple HTTP endpoints.
Zabbix polls these via HTTP agent items.
When real ATM arrives: point Zabbix items to real ATM IP. Done.
"""

import os
import time
import random
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

ATM_ID = os.environ.get('ATM_ID', 'ATM-001')
ATM_BRANCH = os.environ.get('ATM_BRANCH', 'Addis Ababa Main Branch')
ATM_TERMINAL_ID = os.environ.get('ATM_TERMINAL_ID', 'TID001')
SNMP_PORT = int(os.environ.get('SNMP_PORT', '1161'))

# Full ATM hardware state
state = {
    'atm_status': 1,
    'cassette1': random.randint(1500, 2500),
    'cassette2': random.randint(1200, 2000),
    'cassette3': random.randint(1000, 1800),
    'cassette4': random.randint(800, 1500),
    'reject_bin': random.randint(0, 15),
    'cash_jam': 0,
    'partial_dispense': 0,
    'card_reader': 1,
    'card_captures': 0,
    'shutter': 1,
    'receipt_printer': 1,
    'receipt_paper': random.randint(60, 100),
    'journal_printer': 1,
    'safe_door': 0,
    'cabinet_door': 0,
    'temperature': random.randint(22, 28),
    'humidity': random.randint(40, 60),
    'vibration': 0,
    'intrusion': 0,
    'txn_total': 0,
    'txn_failed': 0,
    'txn_success': 0,
    'last_error': 0,
    'main_power': 1,
    'ups_status': 1,
    'ups_battery': random.randint(85, 100),
    'last_power_event': 0,
    'net_link': 1,
    'net_latency': random.randint(10, 50),
    'packet_loss': 0,
    'link_type': 1,
    'camera1': 1,
    'camera2': 1,
    'cam_storage': 1,
}

OID_MAP = {
    '1.1.0': 'atm_status',
    '1.2.0': 'cassette1',
    '1.3.0': 'cassette2',
    '1.4.0': 'cassette3',
    '1.5.0': 'cassette4',
    '1.6.0': 'reject_bin',
    '1.7.0': 'cash_jam',
    '1.8.0': 'partial_dispense',
    '2.1.0': 'card_reader',
    '2.2.0': 'card_captures',
    '2.3.0': 'shutter',
    '3.1.0': 'receipt_printer',
    '3.2.0': 'receipt_paper',
    '3.3.0': 'journal_printer',
    '4.1.0': 'safe_door',
    '4.2.0': 'cabinet_door',
    '4.3.0': 'temperature',
    '4.4.0': 'humidity',
    '4.5.0': 'vibration',
    '4.6.0': 'intrusion',
    '5.1.0': 'txn_total',
    '5.2.0': 'txn_failed',
    '5.3.0': 'txn_success',
    '5.4.0': 'last_error',
    '6.1.0': 'main_power',
    '6.2.0': 'ups_status',
    '6.3.0': 'ups_battery',
    '6.4.0': 'last_power_event',
    '7.1.0': 'net_link',
    '7.2.0': 'net_latency',
    '7.3.0': 'packet_loss',
    '7.4.0': 'link_type',
    '8.1.0': 'camera1',
    '8.2.0': 'camera2',
    '8.3.0': 'cam_storage',
}

def simulate():
    while True:
        hour = time.localtime().tm_hour
        if hour in [8,9,12,13,17,18]:
            rate = random.randint(3,8)
        elif hour in [0,1,2,3,4]:
            rate = random.randint(0,1)
        else:
            rate = random.randint(1,4)

        state['txn_total'] += rate + random.randint(0,2)
        fails = random.randint(0,1)
        state['txn_failed'] += fails
        state['txn_success'] = state['txn_total'] - state['txn_failed']

        for k in ['cassette1','cassette2','cassette3','cassette4']:
            state[k] = max(0, state[k] - random.randint(0, rate))

        if all(state[f'cassette{i}'] == 0 for i in range(1,5)):
            state['atm_status'] = 2
        elif state['atm_status'] == 2 and random.random() < 0.05:
            state['atm_status'] = 1

        state['reject_bin'] = min(100, state['reject_bin'] + random.randint(0,1))

        r = random.random()
        if r < 0.003:
            state['cash_jam'] = 1
            state['atm_status'] = 3
            print(f"[{ATM_ID}] CASH JAM detected")
        elif state['cash_jam'] == 1 and r < 0.05:
            state['cash_jam'] = 0
            state['atm_status'] = 1

        if r < 0.002:
            state['card_reader'] = 2
        elif state['card_reader'] == 2 and r < 0.1:
            state['card_reader'] = 1

        state['receipt_paper'] = max(0, state['receipt_paper'] - random.randint(0,1))
        if state['receipt_paper'] < 20:
            state['receipt_printer'] = 2
        if state['receipt_paper'] == 0:
            state['receipt_printer'] = 3

        state['temperature'] += random.randint(-1, 1)
        state['temperature'] = max(18, min(45, state['temperature']))

        if r < 0.005:
            state['net_latency'] = random.randint(200, 2000)
            state['packet_loss'] = random.randint(5, 30)
        elif state['net_latency'] > 100 and r < 0.1:
            state['net_latency'] = random.randint(10, 50)
            state['packet_loss'] = 0

        if r < 0.001:
            state['cabinet_door'] = 1
            print(f"[{ATM_ID}] Cabinet door OPENED")
        elif state['cabinet_door'] == 1 and r < 0.2:
            state['cabinet_door'] = 0

        if r < 0.002:
            state['camera1'] = 2
        elif state['camera1'] == 2 and r < 0.1:
            state['camera1'] = 1

        time.sleep(30)

threading.Thread(target=simulate, daemon=True).start()

class ATMHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            metrics = {oid: state[key] for oid, key in OID_MAP.items()}
            metrics['atm_id'] = ATM_ID
            metrics['branch'] = ATM_BRANCH
            metrics['terminal_id'] = ATM_TERMINAL_ID
            metrics['timestamp'] = int(time.time())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(metrics).encode())

        elif self.path.startswith('/oid/'):
            oid = self.path.replace('/oid/', '')
            key = OID_MAP.get(oid)
            if key:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(str(state[key]).encode())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'OID not found')

        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')

        else:
            self.send_response(404)
            self.end_headers()

print(f"[{ATM_ID}] ATM Simulator starting")
print(f"[{ATM_ID}] Branch: {ATM_BRANCH} | Terminal: {ATM_TERMINAL_ID}")
print(f"[{ATM_ID}] Metrics API: http://0.0.0.0:{SNMP_PORT}/metrics")
print(f"[{ATM_ID}] OID endpoint: http://0.0.0.0:{SNMP_PORT}/oid/1.1.0")

server = HTTPServer(('0.0.0.0', SNMP_PORT), ATMHandler)
print(f"[{ATM_ID}] Ready and listening on port {SNMP_PORT}")
server.serve_forever()
