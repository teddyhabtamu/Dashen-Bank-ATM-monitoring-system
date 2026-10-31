"""Static contract for the installer wrapper and release build.

These run everywhere, including the dependency-free Linux suite. The apply
path lives in test_agent_installer.py and runs on Windows with pwsh.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = REPO / 'installer'


def test_the_inno_wrapper_runs_the_payload_and_propagates_failure():
    spec = (INSTALLER / 'AtmMonitorSetup.iss').read_text()
    assert 'PrivilegesRequired=admin' in spec
    assert 'atm-install.ps1' in spec
    assert 'PROFILE' in spec and 'DRYRUN' in spec
    assert 'ResultCode' in spec
    assert 'Abort' in spec
    assert 'Uninstallable=no' in spec
    for flag in ('ALLOWNOTREADY', 'REPLACELEGACY', 'NOAGENTINSTALL',
                 'NOSERVICE', 'REASSIGN', 'EXPECTEDHASH', 'ACCEPTATM'):
        assert flag in spec


def test_the_build_script_pins_the_manifest_and_agent_msi():
    build = (INSTALLER / 'build_agent_setup.ps1').read_text()
    assert 'agent_manifest.json' in build
    assert 'zabbix_agent2' in build
    assert 'PinnedAgentMsi' in build
    assert '853a8ed21fd3cb14b4ffacda61e3c48165179025bf90c61b40b61d847a16bbd7' in build
    assert 'ISCC.exe' in build or 'iscc' in build.lower()
    assert 'signtool' in build.lower()
    assert 'RequireSignature' in build


def test_the_config_check_uses_agent2_flags():
    """The pinned Agent 2 6.4 has no -T; that is the Agent 1 config-test flag.
    Using it makes every install fail with 'flag provided but not defined'
    before the service is touched. -p would probe every known item (minutes on
    a real ATM and it runs user parameters); -t agent.ping parses the same
    config and returns in milliseconds."""
    module = (INSTALLER / 'AtmInstall.psm1').read_text()
    assert '& $Exe -c $Conf -t agent.ping' in module
    assert '& $Exe -c $Conf -p' not in module
    assert '& $Exe -T' not in module


def test_the_payload_exit_codes_are_documented_and_distinct():
    module = (INSTALLER / 'AtmInstall.psm1').read_text()
    for name in ('InvalidProfile', 'NotReady', 'ProfileHashMismatch',
                 'ProbeMismatch', 'UnsignedProfile', 'AtmIdMismatch',
                 'WriteFailed', 'AgentInstallFailed', 'LegacyAgentPresent',
                 'ServiceFailed', 'ConfigTestFailed', 'VerifyFailed'):
        assert f'{name}' in module
    codes = {}
    for line in module.splitlines():
        stripped = line.strip()
        if '=' in stripped and stripped.split()[0].isalpha():
            key, _, value = stripped.partition('=')
            if key.strip() in {
                    'Ok', 'InvalidProfile', 'NotReady', 'ProfileHashMismatch',
                    'ProbeMismatch', 'UnsignedProfile', 'AtmIdMismatch',
                    'WriteFailed', 'AgentInstallFailed', 'LegacyAgentPresent',
                    'ServiceFailed', 'ConfigTestFailed', 'VerifyFailed',
                    'Unexpected'}:
                codes[key.strip()] = value.strip()
    assert len(codes) >= 12
    assert len(set(codes.values())) == len(codes)


def test_the_payload_never_contains_an_snmp_community_or_secret():
    for name in ('AtmInstall.psm1', 'atm-install.ps1'):
        text = (INSTALLER / name).read_text().lower()
        assert 'community' not in text
        assert 'atm_profile_secret' not in text
