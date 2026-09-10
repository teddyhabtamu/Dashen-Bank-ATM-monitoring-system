"""Contract test: fake-switch bytes <-> gateway TCP parser.

Loads scripts/fake_iso8583_switch.build_0210 (sender) and
camel/iso8583_gateway.parse_iso8583_message (production parser) by file
path — no DB, no sockets. If either side of the wire contract drifts,
this fails BEFORE anyone books a firewall ticket.
"""
import importlib.util
import struct
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    if name == 'gateway' and 'psycopg2' not in sys.modules:
        # Parser tests never touch the DB; stub the driver so the suite
        # stays dependency-free (same ethos as test_ej_parser.py).
        sys.modules['psycopg2'] = types.ModuleType('psycopg2')
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def sender():
    return _load('fake_switch',
                 REPO / 'scripts' / 'fake_iso8583_switch.py')


@pytest.fixture(scope='module')
def gateway():
    return _load('gateway',
                 REPO / 'camel' / 'iso8583_gateway.py')


def _parse(gateway, wire):
    # Reader strips the 2-byte header; parser sees the rest.
    assert struct.unpack('>H', wire[:2])[0] == len(wire) - 2
    return gateway.parse_iso8583_message(wire[2:])


def test_roundtrip_approved(sender, gateway):
    gateway.ATMS = [{'id': 'ATM-TEST', 'tid': 'TIDTEST1',
                     'branch': 'Bole'}]
    wire = sender.build_0210(
        tid='TIDTEST1', pan='4000000000001234', stan='123456',
        amount_cents=500000, processing_code='012000',
        response_code='00', auth_code='654321')
    txn = _parse(gateway, wire)
    assert txn['mti'] == '0210'
    assert txn['txn_type'] == 'WITHDRAWAL'
    assert txn['amount'] == 5000.00
    assert txn['card_masked'] == '************1234'
    assert txn['stan'] == '123456'
    assert txn['status'] == 'APPROVED'
    assert txn['auth_code'] == '654321'
    assert txn['source'] == 'ISO8583_REAL'
    assert (txn['atm_id'], txn['terminal_id'],
            txn['branch']) == ('ATM-TEST', 'TIDTEST1', 'Bole')


def test_roundtrip_declined_no_auth_field(sender, gateway):
    gateway.ATMS = []
    wire = sender.build_0210(
        tid='TIDTEST1', pan='5200000000005678', stan='000007',
        amount_cents=100000, processing_code='012000',
        response_code='51', auth_code=None)
    txn = _parse(gateway, wire)
    assert txn['status'] == 'DECLINED'
    assert txn['auth_code'] is None
    # Unknown TID is stored, flagged — never dropped.
    assert txn['branch'] == 'UNKNOWN'
    assert txn['atm_id'] == 'TIDTEST1'


def test_balance_has_no_amount(sender, gateway):
    gateway.ATMS = []
    wire = sender.build_0210(
        tid='TIDTEST1', pan='4000000000001234', stan='000008',
        amount_cents=None, processing_code='310000',
        response_code='00', auth_code='111222')
    txn = _parse(gateway, wire)
    assert txn['txn_type'] == 'BALANCE_INQ'
    assert txn['amount'] is None


def test_long_tid_rejected(sender):
    with pytest.raises(ValueError, match='field 41'):
        sender.build_0210(
            tid='TID-UAT-NCR', pan='4000000000001234', stan='1',
            amount_cents=100, processing_code='012000',
            response_code='00', auth_code='1')


class _Cursor:
    def __init__(self, rec):
        self.rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        assert 'INSERT INTO atm_transactions' in sql
        self.rec.append(params)


class _Conn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _Cursor(self.rows)

    def commit(self):
        pass


def test_live_socket_through_tcp_handler(sender, gateway):
    """Real TCP: sender bytes -> handle_switch_connection -> store call."""
    import socket
    import threading

    gateway.ATMS = [{'id': 'ATM-TEST', 'tid': 'TIDTEST1',
                     'branch': 'Bole'}]
    db = _Conn()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        client, addr = srv.accept()
        gateway.handle_switch_connection(client, addr, db)
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cli = socket.create_connection(('127.0.0.1', port), timeout=10)
    try:
        cli.sendall(sender.build_0210(
            tid='TIDTEST1', pan='4000000000001234', stan='999999',
            amount_cents=200000, processing_code='012000',
            response_code='00', auth_code='222333'))
        # Split send: header and body apart — TCP may fragment anyway.
        wire = sender.build_0210(
            tid='TIDTEST1', pan='4000000000001234', stan='999998',
            amount_cents=10000, processing_code='310000',
            response_code='00', auth_code='222334')
        cli.sendall(wire[:2])
        cli.sendall(wire[2:10])
        cli.sendall(wire[10:])
    finally:
        cli.close()
    t.join(timeout=10)
    assert not t.is_alive(), 'handler did not exit on disconnect'
    assert len(db.rows) == 2
    first = db.rows[0]
    assert first[0] == 'ATM-TEST'          # atm_id
    assert first[3] == 'WITHDRAWAL'        # txn_type
    assert first[7] == 'APPROVED'          # status
    assert first[14] == 'ISO8583_REAL'      # source
    assert db.rows[1][3] == 'BALANCE_INQ'
