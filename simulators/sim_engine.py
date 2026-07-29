#!/usr/bin/env python3
"""
Multi-tenant ATM metrics simulator (replaces atm-sim-NNN / grg-sim-NNN).

Reads every ATM from `atm_locations`, allocates a sim_port for any new one,
and serves live SNMP-style metrics for ALL of them over HTTP. When a user
registers a new ATM in the Report Portal it automatically appears here on the
next refresh cycle (every ~10s) and starts producing live data.
"""
import os
import time
import random
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import common


NCR_OID = {
    '1.1.0': 'atm_status', '1.2.0': 'cassette1', '1.3.0': 'cassette2',
    '1.4.0': 'cassette3', '1.5.0': 'cassette4', '1.6.0': 'reject_bin',
    '1.7.0': 'cash_jam', '1.8.0': 'partial_dispense', '2.1.0': 'card_reader',
    '2.2.0': 'card_captures', '2.3.0': 'shutter', '3.1.0': 'receipt_printer',
    '3.2.0': 'receipt_paper', '3.3.0': 'journal_printer', '4.1.0': 'safe_door',
    '4.2.0': 'cabinet_door', '4.3.0': 'temperature', '4.4.0': 'humidity',
    '4.5.0': 'vibration', '4.6.0': 'intrusion', '5.1.0': 'txn_total',
    '5.2.0': 'txn_failed', '5.3.0': 'txn_success', '5.4.0': 'last_error',
    '6.1.0': 'main_power', '6.2.0': 'ups_status', '6.3.0': 'ups_battery',
    '6.4.0': 'last_power_event', '7.1.0': 'net_link', '7.2.0': 'net_latency',
    '7.3.0': 'packet_loss', '7.4.0': 'link_type', '8.1.0': 'camera1',
    '8.2.0': 'camera2', '8.3.0': 'cam_storage',
}

GRG_OID = {
    '1.1.0': 'atm_status', '2.1.0': 'cash_module1', '2.2.0': 'cash_module2',
    '2.3.0': 'cash_module3', '2.4.0': 'purge_bin', '2.5.0': 'cash_jam',
    '3.1.0': 'card_unit', '3.2.0': 'card_captures', '4.1.0': 'thermal_printer',
    '4.2.0': 'paper_level', '5.1.0': 'safe_door', '5.2.0': 'top_hat',
    '5.3.0': 'temperature', '5.4.0': 'humidity', '6.1.0': 'txn_total',
    '6.2.0': 'txn_failed', '6.3.0': 'txn_success', '7.1.0': 'ups_status',
    '7.2.0': 'ups_battery', '8.1.0': 'net_link', '8.2.0': 'net_latency',
    '8.3.0': 'packet_loss', '9.1.0': 'camera1', '9.2.0': 'cam_storage',
}


def ncr_state():
    return {
        'atm_status': 1, 'cassette1': random.randint(1500, 2500),
        'cassette2': random.randint(1200, 2000), 'cassette3': random.randint(1000, 1800),
        'cassette4': random.randint(800, 1500), 'reject_bin': random.randint(0, 15),
        'cash_jam': 0, 'partial_dispense': 0, 'card_reader': 1, 'card_captures': 0,
        'shutter': 1, 'receipt_printer': 1, 'receipt_paper': random.randint(60, 100),
        'journal_printer': 1, 'safe_door': 0, 'cabinet_door': 0,
        'temperature': random.randint(22, 28), 'humidity': random.randint(40, 60),
        'vibration': 0, 'intrusion': 0, 'txn_total': 0, 'txn_failed': 0,
        'txn_success': 0, 'last_error': 0, 'main_power': 1, 'ups_status': 1,
        'ups_battery': random.randint(85, 100), 'last_power_event': 0, 'net_link': 1,
        'net_latency': random.randint(10, 50), 'packet_loss': 0, 'link_type': 1,
        'camera1': 1, 'camera2': 1, 'cam_storage': 1,
    }


def grg_state():
    return {
        'atm_status': 1, 'cash_module1': random.randint(1500, 2500),
        'cash_module2': random.randint(1200, 2000), 'cash_module3': random.randint(1000, 1800),
        'purge_bin': random.randint(0, 20), 'cash_jam': 0, 'card_unit': 1,
        'card_captures': 0, 'thermal_printer': 1, 'paper_level': random.randint(60, 100),
        'safe_door': 0, 'top_hat': 0, 'temperature': random.randint(22, 30),
        'humidity': random.randint(40, 65), 'txn_total': 0, 'txn_failed': 0,
        'txn_success': 0, 'ups_status': 1, 'ups_battery': random.randint(85, 100),
        'net_link': 1, 'net_latency': random.randint(15, 60), 'packet_loss': 0,
        'camera1': 1, 'cam_storage': 1,
    }


def simulate(state, vendor, atm_id):
    hour = time.localtime().tm_hour
    if hour in (8, 9, 12, 13, 17, 18):
        rate = random.randint(3, 8)
    elif hour in (0, 1, 2, 3, 4):
        rate = random.randint(0, 1)
    else:
        rate = random.randint(1, 4)

    state['txn_total'] += rate + random.randint(0, 2)
    state['txn_failed'] += random.randint(0, 1)
    state['txn_success'] = state['txn_total'] - state['txn_failed']

    r = random.random()
    if vendor == 'GRG':
        for k in ('cash_module1', 'cash_module2', 'cash_module3'):
            state[k] = max(0, state[k] - random.randint(0, rate))
        if all(state[f'cash_module{i}'] == 0 for i in range(1, 4)):
            # Out of cash -> cash replenishment (CIT restock), else stay down briefly
            if r < 0.4:
                for i in range(1, 4):
                    state[f'cash_module{i}'] = random.randint(1800, 2500)
                state['atm_status'] = 1
            else:
                state['atm_status'] = 2
        elif state['atm_status'] == 2 and r < 0.05:
            state['atm_status'] = 1
        if r < 0.003:
            state['cash_jam'] = 1; state['atm_status'] = 3
        elif state['cash_jam'] == 1 and r < 0.05:
            state['cash_jam'] = 0; state['atm_status'] = 1
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
    else:
        for k in ('cassette1', 'cassette2', 'cassette3', 'cassette4'):
            state[k] = max(0, state[k] - random.randint(0, rate))
        if all(state[f'cassette{i}'] == 0 for i in range(1, 5)):
            # Out of cash -> cash replenishment (CIT restock), else stay down briefly
            if r < 0.4:
                for i in range(1, 5):
                    state[f'cassette{i}'] = random.randint(1800, 2500)
                state['atm_status'] = 1
            else:
                state['atm_status'] = 2
        elif state['atm_status'] == 2 and r < 0.05:
            state['atm_status'] = 1
        state['reject_bin'] = min(100, state['reject_bin'] + random.randint(0, 1))
        if r < 0.003:
            state['cash_jam'] = 1; state['atm_status'] = 3
        elif state['cash_jam'] == 1 and r < 0.05:
            state['cash_jam'] = 0; state['atm_status'] = 1
        if r < 0.002:
            state['card_reader'] = 2
        elif state['card_reader'] == 2 and r < 0.1:
            state['card_reader'] = 1
        state['receipt_paper'] = max(0, state['receipt_paper'] - random.randint(0, 1))
        if state['receipt_paper'] < 20:
            state['receipt_printer'] = 2
        if state['receipt_paper'] == 0:
            state['receipt_printer'] = 3
        state['temperature'] = max(18, min(45, state['temperature'] + random.randint(-1, 1)))
        if r < 0.001:
            state['cabinet_door'] = 1
        elif state['cabinet_door'] == 1 and r < 0.2:
            state['cabinet_door'] = 0
        if r < 0.002:
            state['camera1'] = 2
        elif state['camera1'] == 2 and r < 0.1:
            state['camera1'] = 1
    time.sleep(30)


class ATMInstance:
    def __init__(self, atm):
        self.atm = atm
        self.state = grg_state() if atm['vendor'] == 'GRG' else ncr_state()
        self.oid = GRG_OID if atm['vendor'] == 'GRG' else NCR_OID
        self._stop = threading.Event()
        self.httpd = None

    def start(self, retries=6, delay=1.0):
        threading.Thread(target=self._sim_loop, daemon=True).start()
        self.httpd = None

        def handler_factory(inst):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):
                    if self.path == '/metrics':
                        m = {oid: inst.state[key] for oid, key in inst.oid.items()}
                        m.update({'atm_id': inst.atm['atm_id'], 'branch': inst.atm['branch'],
                                  'vendor': inst.atm['vendor'],
                                  'terminal_id': inst.atm['terminal_id'],
                                  'timestamp': int(time.time())})
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps(m).encode())
                    elif self.path.startswith('/oid/'):
                        oid = self.path.replace('/oid/', '')
                        key = inst.oid.get(oid)
                        if key is not None:
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/plain')
                            self.end_headers()
                            self.wfile.write(str(inst.state[key]).encode())
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
            return H

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                httpd = ThreadingHTTPServer(('0.0.0.0', self.atm['port']), handler_factory(self))
            except OSError as e:
                # Port held by a lingering process (e.g. just-restarted engine).
                # Retry with backoff instead of silently dropping the ATM.
                last_err = e
                print(f"[SIM] {self.atm['atm_id']} bind :{self.atm['port']} failed "
                      f"(attempt {attempt}/{retries}): {e}")
                time.sleep(delay)
                continue
            self.httpd = httpd
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            print(f"[SIM] {self.atm['atm_id']} ({self.atm['vendor']}) serving on :{self.atm['port']}")
            # Also serve the same OID values over real SNMP (UDP, same port) so
            # Zabbix can poll with SNMP-agent items (production-accurate path).
            try:
                import snmp_agent
                snmp_agent.start_snmp(
                    self.atm['atm_id'], self.atm['port'], self.oid, self.state
                )
            except Exception as e:
                print(f"[SIM] {self.atm['atm_id']} SNMP start skipped: {e}")
            return True
        print(f"[SIM] {self.atm['atm_id']} FAILED to bind :{self.atm['port']} "
              f"after {retries} attempts ({last_err})")
        return False

    def stop(self):
        self._stop.set()
        if getattr(self, 'httpd', None):
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass

    def healthy(self):
        """True if this ATM's HTTP server is actually answering."""
        if not getattr(self, 'httpd', None):
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.atm['port']}/health",
                                        timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _sim_loop(self):
        while not self._stop.is_set():
            try:
                simulate(self.state, self.atm['vendor'], self.atm['atm_id'])
            except Exception as e:
                print(f"[SIM] {self.atm['atm_id']} sim error: {e}")
                time.sleep(5)


def main():
    registry = {}

    def sync():
        conn = common.get_db()
        try:
            atms = common.refresh(conn)
        finally:
            conn.close()

        active_ids = {a['atm_id'] for a in atms}

        # Remove threads for ATMs that are no longer active
        for aid in list(registry.keys()):
            if aid not in active_ids:
                print(f"[SIM] Removing inactive ATM {aid} from registry")
                registry[aid].stop()
                del registry[aid]

        # Self-heal: any previously-registered ATM whose server stopped
        # answering gets torn down so it is recreated (and re-bound) below.
        for aid, inst in list(registry.items()):
            if not inst.healthy():
                print(f"[SIM] {aid} health check failed — restarting simulator")
                inst.stop()
                del registry[aid]

        for a in atms:
            inst = registry.get(a['atm_id'])
            if inst is None:
                inst = ATMInstance(a)
                if inst.start():
                    registry[a['atm_id']] = inst
                # if start() failed to bind, leave it out so sync() retries next cycle
            elif inst.atm['port'] != a['port']:
                # DB sim_port changed (e.g. reallocation) -> rebind on the new port
                print(f"[SIM] {a['atm_id']} port changed "
                      f"{inst.atm['port']} -> {a['port']}, rebinding")
                inst.stop()
                del registry[a['atm_id']]
                inst = ATMInstance(a)
                if inst.start():
                    registry[a['atm_id']] = inst

    for _ in range(30):
        try:
            common.get_db().close()
            print("Connected to PostgreSQL")
            break
        except Exception:
            print("Waiting for DB...")
            time.sleep(5)

    print("Multi-tenant ATM Sim Engine started — auto-simulating all ATMs")
    while True:
        try:
            sync()
        except Exception as e:
            print(f"[SIM] sync error: {e}")
        time.sleep(10)


if __name__ == '__main__':
    main()
