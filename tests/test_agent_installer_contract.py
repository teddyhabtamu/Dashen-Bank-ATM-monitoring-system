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


def test_the_wrapper_explains_a_missing_profile_instead_of_exiting_silently():
    """With no *.profile.json beside the EXE, FindFirst fails and the old
    wrapper returned before reaching the "No .profile.json" message, so the
    installer vanished after UAC with no explanation."""
    spec = (INSTALLER / 'AtmMonitorSetup.iss').read_text()
    findfirst = spec[spec.index('if not FindFirst'):]
    branch = findfirst[:findfirst.index('try')]
    assert 'No .profile.json was found' in branch


def test_the_verification_task_is_allowed_to_run_on_battery():
    """schtasks defaults to 'No Start On Batteries'; on a laptop the SYSTEM
    verification task then sits Queued until the installer times out."""
    module = (INSTALLER / 'AtmInstall.psm1').read_text()
    assert 'AllowStartIfOnBatteries' in module
    assert 'DontStopIfGoingOnBatteries' in module


def test_service_control_failures_map_to_the_service_exit_code():
    """Start-Service throws "Failed to start service '...'", which matched no
    branch and fell through to WriteFailed (20). A technician then reads a
    write error for a service that simply refused to start."""
    script = (INSTALLER / 'atm-install.ps1').read_text()
    mapping = script[script.index('function Get-ExitCodeForError'):]
    mapping = mapping[:mapping.index('\n}')]
    assert 'Failed to (start|stop|restart) service' in mapping
    assert 'Timeout while waiting for service' in mapping


def test_the_service_restart_waits_for_a_start_pending_msi_service():
    """msiexec can return while the freshly installed service is still
    StartPending. A stop request then fails with 1061, so the restart has to
    wait for Running before calling Stop-Service."""
    module = (INSTALLER / 'AtmInstall.psm1').read_text()
    restart = module[module.index("$Action -eq 'restart'"):]
    assert restart.index("StartPending") < restart.index('Stop-Service')


def test_service_start_is_retried_for_the_known_agent2_startup_bug():
    """ZBX-25530: Agent 2 6.4.21 randomly exits during startup with
    "cannot load os dependent items ... buffer". 6.4 is EOL and has no fix,
    so the installer retries the SCM start instead of failing the whole
    install on the first crash. A persistent failure still names the status."""
    module = (INSTALLER / 'AtmInstall.psm1').read_text()
    assert 'function Start-AgentServiceWithRetry' in module
    assert 'ZBX-25530' in module
    assert 'Start-AgentServiceWithRetry -Name $script:AgentServiceName' in module
    assert 'Failed to start service' in module
    retry = module[module.index('function Start-AgentServiceWithRetry'):]
    retry = retry[:retry.index('function Set-AgentService')]
    assert '$Attempts = 3' in retry
    assert 'Start-Sleep -Seconds 2' in retry


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
