"""Identity is evidence, not authority. Every rejection here protects the registry.

The probe runs on a Windows box that may be misconfigured, half-installed, or
plain wrong. These tests pin the rules that stop a bad document from changing
what the portal believes about an ATM.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

from atm_identity import (  # noqa: E402
    IdentityError, conflicts, normalize, parse, probe_drift, readiness)


def identity(**overrides):
    payload = {
        'schema': 1,
        'collected_at': '2026-10-30T09:00:00Z',
        'computer': 'ATM-PC-01',
        'vendor': 'ncr',
        'vendor_source': 'aptra',
        'model': 'SelfServ 82',
        'serial': 'SN-123',
        'terminal_id': 'TID006',
        'terminal_source': 'ej-header',
        'ej': {
            'hostname': 'ATM-TEST-NCR',
            'hostname_source': 'ej-filename',
            'roots': ['C:\\ATM\\EJ'],
            'newest_file': 'EJ0001.log',
            'format': 'ncr',
        },
        'images': {
            'roots': ['C:\\ATM\\images'],
            'candidates': [{'path': 'C:\\ATM\\images', 'files': 12}],
        },
        'ip_addresses': ['10.41.30.10'],
        'probes': {
            'ncr-xfs-probe.ps1': {'sha256': 'a' * 64, 'version': '2026-10-29.3'},
        },
        'agent': {'version': '6.4.0', 'config_hash': 'b' * 64},
    }
    payload.update(overrides)
    return payload


def test_a_valid_document_is_normalised_to_the_fixed_shape():
    got = normalize(identity())
    assert got['vendor'] == 'NCR'
    assert got['terminal_id'] == 'TID006'
    assert got['terminal_confidence'] == 'high'
    assert got['ej']['hostname'] == 'ATM-TEST-NCR'
    assert got['images']['candidates'] == [{'path': 'C:\\ATM\\images', 'files': 12}]
    assert got['probes']['ncr-xfs-probe.ps1']['version'] == '2026-10-29.3'


def test_unknown_top_level_keys_are_dropped():
    got = normalize(identity(not_a_field='ignore me'))
    assert 'not_a_field' not in got


def test_unknown_is_a_valid_terminal_identity():
    got = normalize(identity(terminal_id=None, terminal_source='unknown'))
    assert got['terminal_id'] is None
    assert got['terminal_source'] == 'unknown'
    assert got['terminal_confidence'] == 'unknown'


@pytest.mark.parametrize('schema', [True, False, '1', 1.0, 2])
def test_only_integer_schema_one_is_accepted(schema):
    with pytest.raises(IdentityError, match='schema'):
        normalize(identity(schema=schema))


@pytest.mark.parametrize('field,value', [
    ('vendor', 'WINCOR'),
    ('terminal_source', 'made-up'),
    ('terminal_id', 'has space'),
    ('terminal_id', 'x' * 21),
    ('model', 'm' * 51),
    ('computer', 'pc\nname'),
])
def test_values_that_could_poison_the_registry_are_rejected(field, value):
    with pytest.raises(IdentityError):
        normalize(identity(**{field: value}))


@pytest.mark.parametrize('field', ['ej', 'images', 'agent'])
def test_falsy_non_object_sections_are_rejected(field):
    with pytest.raises(IdentityError, match='must be an object'):
        normalize(identity(**{field: 0}))


def test_image_candidates_must_be_a_list():
    payload = identity()
    payload['images']['candidates'] = 0
    with pytest.raises(IdentityError, match='candidates must be a list'):
        normalize(payload)


@pytest.mark.parametrize('path', [
    '\\\\server\\share',
    'C:\\',
    'C:\\ATM\\..\\Windows',
    'C:\\ATM\\*.jpg',
    'C:\\ATM\\file.jpg:stream',
    'relative\\path',
])
def test_image_roots_must_be_absolute_plain_directories(path):
    payload = identity()
    payload['images']['roots'] = [path]
    with pytest.raises(IdentityError):
        normalize(payload)


def test_duplicate_roots_are_removed_case_insensitively():
    payload = identity()
    payload['images']['roots'] = ['C:\\ATM\\images', 'c:\\atm\\IMAGES', 'C:\\ATM\\other']
    got = normalize(payload)
    assert got['images']['roots'] == ['C:\\ATM\\images', 'C:\\ATM\\other']


def test_probe_hashes_must_be_sha256():
    payload = identity()
    payload['probes']['ncr-xfs-probe.ps1']['sha256'] = 'not-a-hash'
    with pytest.raises(IdentityError, match='SHA-256'):
        normalize(payload)


def test_agent_profile_hash_is_optional_and_normalised():
    """An older probe omits it; the fleet then asks for a reapply instead of
    inventing convergence. A reported hash is lowercase SHA-256 evidence."""
    payload = identity()
    assert normalize(payload)['agent']['profile_hash'] is None
    assert normalize(payload)['agent']['deploy_hash'] is None

    payload['agent']['profile_hash'] = 'C' * 64
    payload['agent']['deploy_hash'] = 'D' * 64
    payload['agent']['applied_at'] = '2026-10-31T09:00:00Z'
    got = normalize(payload)
    assert got['agent']['profile_hash'] == 'c' * 64
    assert got['agent']['deploy_hash'] == 'd' * 64
    assert got['agent']['applied_at'] == '2026-10-31T09:00:00Z'


def test_a_malformed_agent_profile_hash_is_rejected():
    payload = identity()
    payload['agent']['profile_hash'] = 'not-a-hash'
    with pytest.raises(IdentityError, match='profile_hash'):
        normalize(payload)


def test_a_malformed_agent_deploy_hash_is_rejected():
    payload = identity()
    payload['agent']['deploy_hash'] = 'not-a-hash'
    with pytest.raises(IdentityError, match='deploy_hash'):
        normalize(payload)


def test_agent_applied_at_must_be_iso8601():
    payload = identity()
    payload['agent']['applied_at'] = 'yesterday'
    with pytest.raises(IdentityError, match='applied_at'):
        normalize(payload)


def test_parse_accepts_a_json_string_and_rejects_garbage():
    assert parse('{"schema": 1}')['schema'] == 1
    with pytest.raises(IdentityError, match='not valid JSON'):
        parse('{nope')


def test_conflicts_name_the_field_and_keep_the_registry_value():
    row = {
        'vendor': 'GRG', 'terminal_id': 'TID999', 'ej_hostname': 'OLD-NAME',
        'model': 'Old model', 'ip_address': '10.0.0.9',
    }
    got = conflicts(normalize(identity()), row)
    fields = {c['field']: c for c in got}
    assert fields['vendor']['registered'] == 'GRG'
    assert fields['vendor']['reported'] == 'NCR'
    assert fields['vendor']['severity'] == 'high'
    assert fields['terminal_id']['severity'] == 'high'
    assert fields['ej_hostname']['severity'] == 'medium'
    assert fields['model']['severity'] == 'low'
    assert fields['ip_address']['reported'] == '10.41.30.10'


def test_an_unknown_reported_value_never_creates_a_conflict():
    row = {'vendor': 'NCR', 'terminal_id': 'TID006', 'ej_hostname': 'ATM-TEST-NCR',
           'model': 'SelfServ 82', 'ip_address': '10.41.30.10'}
    payload = identity(terminal_id=None, terminal_source='unknown')
    payload['ej']['hostname'] = None
    payload['model'] = None
    assert conflicts(normalize(payload), row) == []


def test_readiness_lists_every_observed_root_when_none_are_approved():
    got = readiness(normalize(identity()), [])
    assert got['images_ready'] is True
    assert got['images_unapproved'] == ['C:\\ATM\\images']


def test_readiness_compares_roots_case_insensitively():
    got = readiness(normalize(identity()), ['c:\\atm\\IMAGES'])
    assert got['images_unapproved'] == []


def test_readiness_reports_ej_and_probe_state():
    got = readiness(normalize(identity()))
    assert got['ej_ready'] is True
    assert got['probes_reported'] is True


def test_probe_drift_reports_missing_hash_and_version_separately():
    expected = {
        'a.ps1': {'sha256': 'a' * 64, 'version': '1'},
        'b.ps1': {'sha256': 'b' * 64, 'version': '2'},
        'c.ps1': {'sha256': 'c' * 64, 'version': '3'},
    }
    reported = {
        'a.ps1': {'sha256': 'a' * 64, 'version': '1'},
        'b.ps1': {'sha256': 'f' * 64, 'version': '2'},
        'c.ps1': {'sha256': 'c' * 64, 'version': '9'},
        'd.ps1': {'sha256': 'd' * 64, 'version': '1'},
    }
    got = probe_drift({'probes': reported}, expected)
    assert got['missing'] == []
    assert got['hash_mismatch'] == ['b.ps1']
    assert got['version_mismatch'] == ['c.ps1']
    assert got['unexpected'] == ['d.ps1']
    assert got['ok'] is False


def test_probe_drift_is_ok_when_every_expected_hash_matches():
    expected = {'a.ps1': {'sha256': 'a' * 64, 'version': '1'}}
    got = probe_drift({'probes': {'a.ps1': {'sha256': 'a' * 64, 'version': '1'}}}, expected)
    assert got['ok'] is True


def test_probe_drift_treats_a_missing_hash_as_unverified():
    expected = {'a.ps1': {'sha256': 'a' * 64, 'version': '1'}}
    got = probe_drift({'probes': {'a.ps1': {'sha256': None, 'version': '1'}}}, expected)
    assert got['hash_mismatch'] == ['a.ps1']
    assert got['ok'] is False
