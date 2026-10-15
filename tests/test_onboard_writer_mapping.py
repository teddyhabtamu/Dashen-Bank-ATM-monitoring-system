"""Tests for onboard_rules.to_atm_data — the wizard -> shared writer mapping.

This mapping is the seam between two entry points (the wizard and ATM
Registration). It shipped a silent failure: the writer requires a `status`
column, the wizard never collected it, and the validation error landed on a
field the wizard page cannot display — so "Save ATM" did nothing and the page
looked unchanged. These tests pin the defaults so that class of failure cannot
return silently.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'report-portal'))

from onboard_rules import WRITER_DEFAULTS, to_atm_data  # noqa: E402

FACTS = {'atm_id': 'Test1', 'atm_name': 'Test1', 'branch': 'Test1', 'district': 'Kirkos',
         'city': 'Addis Ababa', 'region': 'Addis Ababa', 'terminal_id': 'Test1',
         'vendor': 'NCR', 'model': 'SelfServ 34', 'ip_address': '10.30.41.116'}

# Mirrors blueprints.admin._validate's required list. Kept as a literal here on
# purpose: if someone changes the writer's contract and forgets the wizard,
# this test is what fails.
WRITER_REQUIRED = ('atm_id', 'branch', 'district', 'city', 'region', 'terminal_id',
                   'vendor', 'model', 'status')


def test_every_field_the_writer_requires_is_present():
    """The regression: status was required by the writer and missing here."""
    data = to_atm_data(FACTS)
    for field in WRITER_REQUIRED:
        assert data.get(field), f'{field} missing from the wizard payload'


def test_status_defaults_to_active():
    assert to_atm_data(FACTS)['status'] == 'active'


def test_an_explicit_status_is_not_overwritten():
    data = to_atm_data(dict(FACTS, status='inactive'))
    assert data['status'] == 'inactive'


def test_defaults_are_declared_not_magic():
    assert 'status' in WRITER_DEFAULTS
    assert WRITER_DEFAULTS['status'] == 'active'


def test_blank_values_become_none_not_empty_strings():
    data = to_atm_data(dict(FACTS, latitude='', longitude='', atm_name=None))
    assert data['latitude'] is None and data['longitude'] is None
    assert data['atm_name'] is None


def test_values_are_trimmed():
    assert to_atm_data(dict(FACTS, branch='  4 Kilo  '))['branch'] == '4 Kilo'


def test_optional_coordinates_pass_through():
    data = to_atm_data(dict(FACTS, latitude='9.0054', longitude='38.7636'))
    assert data['latitude'] == '9.0054' and data['longitude'] == '38.7636'


def test_missing_facts_are_none_so_validation_still_speaks():
    """Defaulting status must not paper over genuinely missing required data."""
    data = to_atm_data({'atm_id': 'Test1'})
    assert data['branch'] is None and data['city'] is None
    assert data['status'] == 'active', 'only status is defaulted'


def test_optional_fields_not_collected_by_the_wizard():
    """The writer tolerates these as None; they must not be invented."""
    data = to_atm_data(FACTS)
    for field in ('install_date', 'snmp_community', 'snmp_version', 'ej_log_format'):
        assert field not in data or data[field] is None