"""The agent profile is the installer's contract, so the tests pin the contract.

A profile that silently omits a probe, leaks a secret, or signs a different
body than it serves is worse than no profile: the installer would apply it and
the box would look configured. These tests build real profiles from the
committed manifest and assert the properties the installer depends on.
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))

import agent_profile  # noqa: E402
from agent_profile import ProfileError, build_profile, load_manifest, verify  # noqa: E402

ATM = {
    'atm_id': 'ATM-TEST-NCR',
    'atm_name': 'Test ATM',
    'vendor': 'NCR',
    'model': 'NCR SelfServ 82',
    'district': 'NAD',
    'city': 'Addis Ababa',
    'ip_address': '10.41.30.10',
    'terminal_id': 'TID006',
    'ej_hostname': 'ATM-TEST-NCR',
    'ej_log_format': 'ncr',
    'image_roots': ['C:\\ATM\\images'],
}

GRG = dict(ATM, atm_id='ATM-TEST-GRG', vendor='GRG', model='GRG H68N',
           terminal_id='TID007', image_roots=[])


def build(row=None, **kwargs):
    kwargs.setdefault('secret', 'test-secret')
    return build_profile(row or ATM, load_manifest(), **kwargs)


def test_the_committed_manifest_matches_the_probe_files():
    out = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'build_agent_manifest.py'), '--check'],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout


def test_district_proxy_map_matches_the_provisioning_script():
    """The portal duplicates the map because scripts/ is not on its path."""
    tree = ast.parse((REPO / 'scripts' / 'sync_atms_to_zabbix.py').read_text())
    mapping = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(target, 'id', None) == 'DISTRICT_PROXY'
               for target in node.targets):
            mapping = ast.literal_eval(node.value)
            break
    assert mapping == agent_profile.DISTRICT_PROXY


def test_ncr_profile_carries_the_expected_probes_and_parameters():
    profile = build()
    names = {p['name'] for p in profile['probes']}
    assert {'atm-wmi-collector.ps1', 'ncr-xfs-probe.ps1'} <= names
    assert 'grg-xfs-probe.ps1' not in names
    params = '\n'.join(profile['user_parameters'])
    assert 'UserParameter=atm.part[*]' in params
    assert 'UserParameter=atm.parts[*]' not in params, (
        'the plural spelling makes the Zabbix agent refuse to start')
    assert 'UserParameter=atm.xfs.cdm' in params
    assert 'UserParameter=atm.image.info[*]' in params
    assert profile['ready'] is True
    assert profile['agent']['server'] == 'zabbix-proxy-addis-north'
    assert profile['agent']['server_source'] == 'district-proxy'


def test_grg_profile_uses_the_grg_probe_and_omits_ncr_parameters():
    profile = build(GRG)
    names = {p['name'] for p in profile['probes']}
    assert 'grg-xfs-probe.ps1' in names
    assert 'ncr-xfs-probe.ps1' not in names
    params = '\n'.join(profile['user_parameters'])
    assert 'grg-xfs-probe.ps1' in params
    assert 'atm.part[*]' not in params
    assert profile['image_roots'] == []
    assert any('No approved image roots' in w for w in profile['warnings'])


def test_profile_is_deterministic_and_verifies():
    first = build()
    second = build()
    assert first['profile_hash'] == second['profile_hash']
    assert first['signature'] == second['signature']
    assert verify(first, 'test-secret') is True
    assert verify(first, 'wrong-secret') is False


def test_profile_has_no_render_timestamp_so_the_download_hash_is_stable():
    first = build()
    second = build()
    assert 'generated_at' not in first
    assert json.dumps(first, indent=2) == json.dumps(second, indent=2)


def test_tampering_with_the_body_breaks_the_signature():
    profile = build()
    profile['terminal_id'] = 'TID999'
    assert verify(profile, 'test-secret') is False


def test_a_non_string_signature_is_rejected_without_raising():
    profile = build()
    profile['signature'] = 123
    assert verify(profile, 'test-secret') is False


def test_the_profile_never_contains_the_signing_secret_or_snmp_community():
    profile = build()
    rendered = json.dumps(profile)
    assert 'test-secret' not in rendered
    assert 'community' not in rendered.lower()
    assert profile['signature'].startswith('hmac-sha256:')


def test_an_unset_secret_produces_an_unsigned_profile():
    profile = build(secret=None)
    assert profile['signature'] is None
    assert verify(profile, None) is False


def test_missing_server_is_a_warning_and_not_ready(monkeypatch):
    monkeypatch.delenv('ATM_AGENT_SERVER', raising=False)
    row = dict(ATM, district='UNKNOWN-DISTRICT')
    profile = build(row)
    assert profile['ready'] is False
    assert profile['agent']['server'] is None
    assert any('Agent server is not configured' in w for w in profile['warnings'])


def test_explicit_server_override_wins(monkeypatch):
    monkeypatch.setenv('ATM_AGENT_SERVER', 'zabbix-proxy-lab')
    profile = build()
    assert profile['agent']['server'] == 'zabbix-proxy-lab'
    assert profile['agent']['server_source'] == 'env:ATM_AGENT_SERVER'


def test_an_invalid_approved_root_refuses_to_render_a_profile():
    row = dict(ATM, image_roots=['\\\\server\\share'])
    with pytest.raises(ProfileError, match='approved image roots are invalid'):
        build(row)


def test_a_vendor_without_a_probe_set_gets_no_profile():
    with pytest.raises(ProfileError, match='has no agent profile'):
        build(dict(ATM, vendor='WINCOR'))


def test_a_missing_manifest_is_an_actionable_error(tmp_path):
    with pytest.raises(ProfileError, match='build_agent_manifest'):
        load_manifest(tmp_path / 'missing.json')


def test_the_agent_log_stays_beside_the_probes():
    config = build()['agent']['config_text']
    assert 'LogType=file' in config
    assert 'LogFile=C:\\monitor\\logs\\zabbix_agent2.log' in config
    assert 'DebugLevel=3' in config


def test_the_agent_config_uses_agent2_directives_only():
    config = build()['agent']['config_text']
    directives = {
        line.split('=', 1)[0].strip()
        for line in config.splitlines()
        if '=' in line and not line.lstrip().startswith('#')
    }
    assert 'EnableRemoteCommands' not in directives
    assert 'LogRemoteCommands' not in directives
    assert 'DenyKey' in directives
    assert 'DenyKey=system.run[*]' in config


def test_the_profile_files_are_written_to_the_expected_windows_paths():
    profile = build()
    destinations = {f['dest'] for f in profile['files']}
    assert agent_profile.AGENT_INCLUDE in destinations
    assert agent_profile.ROOTS_FILE in destinations
    roots = next(f for f in profile['files'] if f['dest'] == agent_profile.ROOTS_FILE)
    assert 'C:\\ATM\\images' in roots['content']
    include = next(f for f in profile['files'] if f['dest'] == agent_profile.AGENT_INCLUDE)
    assert 'Generated by the Dashen ATM portal' in include['content']
    assert 'Do not edit by hand' in include['content']


def test_identity_probe_is_added_only_when_the_manifest_has_it():
    profile = build()
    names = {p['name'] for p in profile['probes']}
    if agent_profile.IDENTITY_PROBE in load_manifest()['files']:
        assert agent_profile.IDENTITY_PROBE in names
    else:
        assert agent_profile.IDENTITY_PROBE not in names
        assert any('atm.identity probe is not in the manifest' in w
                   for w in profile['warnings'])
