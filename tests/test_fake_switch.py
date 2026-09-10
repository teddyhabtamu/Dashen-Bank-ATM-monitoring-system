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
