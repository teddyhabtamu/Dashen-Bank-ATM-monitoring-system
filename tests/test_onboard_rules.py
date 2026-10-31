"""Unit tests for report-portal/onboard_rules.py (dependency-free).

The wizard's guardrails are safety logic: unknown vendor/model/district
must STOP loudly, never misroute silently (the sync script's silent-skip
class, retired by design). Every rule below encodes a spec sentence from
docs/onboarding-wizard-spec.md.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'report-portal'))

from onboard_rules import VENDORS, DISTRICTS, PROBE_PORT, validate_facts


def _good(**over):
    f = {'atm_id': 'ATM-0107', 'atm_name': '', 'branch': '4 Kilo',
         'district': 'Kirkos', 'city': 'Addis Ababa', 'region': 'Addis',
         'latitude': '9.005', 'longitude': '38.789', 'terminal_id': 'TID-107',
         'vendor': 'NCR', 'model': 'SelfServ 34', 'ip_address': '172.23.0.101'}
    f.update(over)
    return f


def test_happy_path():
    c, errors = validate_facts(_good())
    assert errors == {}
    assert c['atm_id'] == 'ATM-0107'


def test_unknown_vendor_stops():
    _, errors = validate_facts(_good(vendor='HYOSUNG'))
    assert 'vendor' in errors


def test_unknown_model_stops():
    _, errors = validate_facts(_good(model='SelfServ 88'))
    assert 'model' in errors


def test_unknown_district_stops():
    _, errors = validate_facts(_good(district='Nowhere'))
    assert 'district' in errors


def test_blank_atm_id_is_allowed_because_the_writer_generates_it():
    c, errors = validate_facts(_good(atm_id=''))
    assert 'atm_id' not in errors
    assert c['atm_id'] == ''
    assert c['terminal_id'] == 'TID-107'


def test_bad_ids_and_ips_stop():
    _, e1 = validate_facts(_good(atm_id='bad id!'))
    assert 'atm_id' in e1
    _, e2 = validate_facts(_good(ip_address='999.1.1.1'))
    assert 'ip_address' in e2


def test_required_fields():
    _, errors = validate_facts(_good(branch='', terminal_id=''))
    assert 'branch' in errors and 'terminal_id' in errors


def test_probe_map_ncr_only():
    assert PROBE_PORT == {'NCR': 10050, 'GRG': 10050}
    assert set(VENDORS) == {'NCR', 'GRG'}
    assert 'Kirkos' in DISTRICTS
