"""The identity probe runs on the oldest ATMs, so its contract is pinned.

PowerShell 2.0 on the NCR test core has no ConvertTo-Json, Get-FileHash or
Get-ChildItem -File. A probe that uses them passes on a developer laptop and
fails silently on the ATM, so these tests read the script instead of running it
(no Windows host in CI).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'report-portal'))
PROBE = REPO / 'scripts' / 'atm-identity.ps1'
SCRIPT = PROBE.read_text()
# Comments name the forbidden constructs on purpose; only executable code is
# checked, or the documentation would fail its own guard.
CODE = re.sub(r'(?s)<#.*?#>', '', SCRIPT)
CODE = re.sub(r'(?m)^\s*#.*$', '', CODE)
MANIFEST = json.loads((REPO / 'report-portal' / 'agent_manifest.json').read_text())
PROFILE = (REPO / 'report-portal' / 'agent_profile.py').read_text()


def test_the_probe_is_powershell_2_compatible():
    assert SCRIPT.startswith('#Requires -Version 2.0')
    for forbidden in (
        r'\bConvertTo-Json\b', r'\bGet-FileHash\b', r'\$PSScriptRoot',
        r'\[Environment\]::Is64BitProcess',
    ):
        assert not re.search(forbidden, CODE), forbidden
    assert not re.search(r'Get-ChildItem[^\n]*-File\b', CODE)


def test_the_probe_reports_every_field_the_portal_normalises():
    for key in ('schema', 'collected_at', 'computer', 'vendor', 'vendor_source',
                'model', 'serial', 'terminal_id', 'terminal_source', 'ej',
                'images', 'ip_addresses', 'probes', 'agent'):
        assert key in SCRIPT, key
    for key in ('hostname', 'hostname_source', 'roots', 'newest_file', 'format'):
        assert key in SCRIPT, key
    for key in ('sha256', 'version', 'config_hash', 'profile_hash',
                'deploy_hash', 'applied_at'):
        assert key in SCRIPT, key


def test_the_probe_emits_one_unwrapped_json_line():
    assert '[System.Console]::WriteLine((ConvertTo-JsonValue $document))' in CODE
    assert 'Write-Output' not in CODE, (
        'Write-Output goes through the formatting pipeline and wraps long JSON')


def test_the_probe_is_read_only():
    for forbidden in (
        'Set-Content', 'Out-File', 'Remove-Item', 'New-Item',
        'Set-ItemProperty', 'Start-Process', 'Stop-Service', 'Set-Service',
        'WFSExecute', 'reg add', 'sc.exe',
    ):
        assert forbidden not in SCRIPT, forbidden
    assert '[IO.FileAccess]::Read' in SCRIPT
    assert '[IO.FileMode]::Open' in SCRIPT


def test_the_probe_uses_the_approved_roots_file_and_never_invents_roots():
    assert "atm-image-roots.txt" in SCRIPT
    assert 'Test-Path -LiteralPath $root -PathType Container' in SCRIPT
    assert 'Get-ImageRoots' in SCRIPT
    assert 'Test-RootRelated' in SCRIPT
    assert "'E:\\pic'" in SCRIPT
    assert "'E:\\EJLogs'" in SCRIPT
    assert 'EJDATA' in SCRIPT


def test_the_probe_reports_the_applied_profile_and_deploy_hashes():
    """The portal can only prove convergence if the box names the profile it
    actually applied. PowerShell 2.0 has no ConvertFrom-Json, so the probe
    reads the two scalars with a regex from the installer's state file."""
    assert 'atm-agent-state.json' in SCRIPT
    assert 'Get-AppliedProfile' in SCRIPT
    assert '"profile_hash"' in SCRIPT
    assert '"deploy_hash"' in SCRIPT
    assert 'ToLowerInvariant' in SCRIPT


def test_the_probe_version_is_in_the_manifest_and_the_profile():
    assert "$probe_version = '2026-10-31.2'" in SCRIPT
    entry = MANIFEST['files']['atm-identity.ps1']
    assert entry['version'] == '2026-10-31.2'
    assert entry['dest'] == 'C:\\monitor\\atm-identity.ps1'
    assert entry['sha256'] == hashlib.sha256(PROBE.read_bytes()).hexdigest()
    assert 'UserParameter=atm.identity' in PROFILE
    assert 'IDENTITY_PROBE' in PROFILE


def test_the_manifest_is_current_after_the_probe_was_added():
    out = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'build_agent_manifest.py'), '--check'],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout


def test_a_probe_shaped_document_passes_the_portal_contract():
    """The keys the PowerShell emits must be accepted without translation."""
    from atm_identity import normalize

    payload = {
        'schema': 1,
        'collected_at': '2026-10-30T09:00:00Z',
        'computer': 'ATM-PC-01',
        'vendor': 'NCR',
        'vendor_source': 'program-files',
        'model': None,
        'serial': 'SN-123',
        'terminal_id': 'PATML056',
        'terminal_source': 'ej-header',
        'ej': {
            'hostname': 'TESTNCR',
            'hostname_source': 'ej-filename',
            'roots': ['E:\\EJLogs'],
            'newest_file': '2026-10-12_TESTNCR_EJDATA.log',
            'format': 'ncr',
        },
        'images': {'roots': ['E:\\pic'], 'candidates': []},
        'ip_addresses': ['10.41.30.10'],
        'probes': {
            'atm-identity.ps1': {
                'sha256': 'a' * 64,
                'version': '2026-10-30.1',
            },
        },
        'agent': {
            'version': '6.4.0',
            'config_hash': 'b' * 64,
            'profile_hash': 'c' * 64,
            'deploy_hash': 'd' * 64,
            'applied_at': '2026-10-31T09:00:00Z',
        },
    }
    got = normalize(payload)
    assert got['vendor'] == 'NCR'
    assert got['terminal_id'] == 'PATML056'
    assert got['terminal_confidence'] == 'high'
    assert got['images']['roots'] == ['E:\\pic']
    assert got['probes']['atm-identity.ps1']['version'] == '2026-10-30.1'
    assert got['agent']['profile_hash'] == 'c' * 64
    assert got['agent']['deploy_hash'] == 'd' * 64
    assert got['agent']['applied_at'] == '2026-10-31T09:00:00Z'
