#!/usr/bin/env python3
"""
GRG ATM Hardware Metrics Simulator
Simulates GRG BRM9/H22N ATM SNMP behavior via HTTP API.
GRG uses different OID structure and fault naming than NCR.
"""
import os, time, random, threading, json
from http.server import HTTPServer, BaseHTTPRequestHandler

ATM_ID = os.environ.get('ATM_ID', 'GRG-001')
ATM_BRANCH = os.environ.get('ATM_BRANCH', 'Bole Atlas Branch')
ATM_TERMINAL_ID = os.environ.get('ATM_TERMINAL_ID', 'TID006')
SNMP_PORT = int(os.environ.get('SNMP_PORT', '1166'))

# GRG state - same fields as NCR but different internal codes
state = {
    'atm_status': 1,          # 1=InService 2=OutOfService 3=Partial 4=Supervisor
    'cash_module1': random.randint(1500, 2500),  # GRG calls them "modules" not "cassettes"
    'cash_module2': random.randint(1200, 2000),
    'cash_module3': random.randint(1000, 1800),
    'purge_bin': random.randint(0, 20),          # GRG calls it "purge bin" not "reject bin"
    'cash_jam': 0,
    'card_unit': 1,            # GRG: "card unit" = NCR: "card reader"
    'card_captures': 0,
    'thermal_printer': 1,      # GRG: "thermal printer" = NCR: "receipt printer"
    'paper_level': random.randint(60, 100),
    'safe_door': 0,
    'top_hat': 0,              # GRG-specific: top hat door
    'temperature': random.randint(22, 30),
    'humidity': random.randint(40, 65),
    'txn_total': 0,
    'txn_failed': 0,
    'txn_success': 0,
    'ups_status': 1,
    'ups_battery': random.randint(85, 100),
    'net_link': 1,
    'net_latency': random.randint(15, 60),
    'packet_loss': 0,
    'camera1': 1,
    'cam_storage': 1,
}

# GRG OID map - different from NCR
OID_MAP = {
    '1.1.0': 'atm_status',
    '2.1.0': 'cash_module1',   # GRG: different OID numbering
    '2.2.0': 'cash_module2',
    '2.3.0': 'cash_module3',
    '2.4.0': 'purge_bin',
    '2.5.0': 'cash_jam',
    '3.1.0': 'card_unit',
    '3.2.0': 'card_captures',
    '4.1.0': 'thermal_printer',
    '4.2.0': 'paper_level',
    '5.1.0': 'safe_door',
    '5.2.0': 'top_hat',
    '5.3.0': 'temperature',
    '5.4.0': 'humidity',
    '6.1.0': 'txn_total',
    '6.2.0': 'txn_failed',
    '6.3.0': 'txn_success',
    '7.1.0': 'ups_status',
    '7.2.0': 'ups_battery',
    '8.1.0': 'net_link',
    '8.2.0': 'net_latency',
    '8.3.0': 'packet_loss',
    '9.1.0': 'camera1',
    '9.2.0': 'cam_storage',
}

def simulate():
    while True:
        hour = time.localtime().tm_hour
        rate = random.randint(3, 8) if hour in [8, 9, 12, 13, 17, 18] else random.randint(1, 3)
        state['txn_total'] += rate
        state['txn_failed'] += random.randint(0, 1)
        state['txn_success'] = state['txn_total'] - state['txn_failed']
        for k in ['cash_module1', 'cash_module2', 'cash_module3']:
            state[k] = max(0, state[k] - random.randint(0, rate))
        if all(state[f'cash_module{i}'] == 0 for i in range(1, 4)):
            state['atm_status'] = 2
        r = random.random()
        if r < 0.003:
            state['cash_jam'] = 1
            state['atm_status'] = 3
        elif state['cash_jam'] == 1 and r < 0.05:
            state['cash_jam'] = 0
            state['atm_status'] = 1
        if r < 0.002:
            state['card_unit'] = 2
        elif state['card_unit'] == 2 and r < 0.1:
            state['card_unit'] = 1
        state['paper_level'] = max(0, state['paper_level'] - random.randint(0, 1))
        if state['paper_level'] < 20:
            state['thermal_printer'] = 2
        state['temperature'] = max(18, min(45, state['temperature'] + random.randint(-1, 1)))
        if r < 0.001:
            state['top_hat'] = 1
        elif state['top_hat'] == 1 and r < 0.2:
            state['top_hat'] = 0
        time.sleep(30)

threading.Thread(target=simulate, daemon=True).start()

class GRGHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            metrics = {oid: state[key] for oid, key in OID_MAP.items()}
            metrics.update({'atm_id': ATM_ID, 'branch': ATM_BRANCH,
                          'vendor': 'GRG', 'terminal_id': ATM_TERMINAL_ID})
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
        elif self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

print(f"[{ATM_ID}] GRG Simulator starting | Branch: {ATM_BRANCH} | Port: {SNMP_PORT}")
HTTPServer(('0.0.0.0', SNMP_PORT), GRGHandler).serve_forever()
