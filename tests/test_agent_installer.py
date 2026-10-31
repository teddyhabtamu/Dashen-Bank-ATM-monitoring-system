"""The installer contract, exercised against real portal-rendered profiles.

The local suite skips these when PowerShell is unavailable. The Windows
installer workflow runs the same file with `pwsh` on PATH, so the apply path
is tested on the platform it ships to, not only on Linux.

What is pinned here:
  * every rejection has its own exit code, so the wrapper can say why;
  * a dry run writes nothing;
  * a second apply is a no-op;
  * the roots file is removed when the registry removes every approved root;
  * a profile for another ATM cannot silently take over a managed root.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = REPO / 'installer' / 'atm-install.ps1'
GENERATOR = REPO / 'installer' / 'make_test_profiles.py'
PWSH = shutil.which('pwsh')

pytestmark = pytest.mark.skipif(PWSH is None, reason='pwsh is not installed')


def _profiles(tmp_path, unsigned=False):
    out = tmp_path / 'profiles'
    monitor = tmp_path / 'monitor'
    args = [sys.executable, str(GENERATOR), '--out', str(out),
            '--monitor-root', str(monitor)]
    if unsigned:
        args.append('--unsigned')
    subprocess.run(args, check=True, capture_output=True, text=True)
    return out, monitor


def _run(*args, timeout=120):
    proc = subprocess.run([PWSH, '-NoProfile', '-File', str(INSTALLER), *args],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


def _common(monitor):
    return ['-MonitorRoot', str(monitor),
            '-ProbeRoot', str(REPO / 'scripts'),
            '-AgentConf', str(monitor / 'agent' / 'zabbix_agent2.conf'),
            '-NoAgentInstall', '-NoService', '-NoServiceAccountTest']


def _result(output):
    for line in output.splitlines():
        if line.startswith('RESULT '):
            return json.loads(line[len('RESULT '):])
    raise AssertionError(f'no RESULT line in output:\n{output}')


def test_both_scripts_parse():
    paths = [REPO / 'installer' / 'AtmInstall.psm1', INSTALLER]
    quoted = ','.join("'" + str(path).replace("'", "''") + "'" for path in paths)
    code = (
        f'$files = @({quoted}); $bad = $false; '
        'foreach ($f in $files) { '
        '$e = $null; $t = $null; '
        '[System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e) | Out-Null; '
        'if ($e) { $bad = $true; $e | ForEach-Object { Write-Error $_.Message } } }; '
        'if ($bad) { exit 1 }'
    )
    proc = subprocess.run([PWSH, '-NoProfile', '-Command', code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_agent2_test_output_is_unwrapped_before_the_json_check():
    """-t prints "<key> [<type>|<value>]", so a JSON user parameter arrives
    wrapped. Treating the raw line as the value made every service-account
    verification fail with an empty message."""
    module = REPO / 'installer' / 'AtmInstall.psm1'
    code = (
        "Import-Module '%s' -Force; "
        "$v = Get-AgentTestValue 'atm.identity [t|{\"ok\":true}]'; "
        "if ($v -ne '{\"ok\":true}') { Write-Error ('unexpected value: ' + $v); exit 1 }; "
        "$k = Get-AgentTestValue 'atm.image.info[abc] [t|{\"x\":[1,2]}]'; "
        "if ($k -ne '{\"x\":[1,2]}') { Write-Error ('unexpected bracketed value: ' + $k); exit 1 }; "
        "$u = Get-AgentTestValue 'atm.identity [m|ZBX_NOTSUPPORTED]'; "
        "if ($u -ne 'ZBX_NOTSUPPORTED') { Write-Error ('unexpected unsupported value: ' + $u); exit 1 }"
    ) % module
    proc = subprocess.run([PWSH, '-NoProfile', '-Command', code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_failed_verification_names_the_failing_check():
    """An empty failure message hid which check broke the install."""
    module = REPO / 'installer' / 'AtmInstall.psm1'
    code = (
        "Import-Module '%s' -Force; "
        "$r = @{ ok = $false; checks = @("
        "@{ name = 'key:atm.identity'; ok = $false; detail = \"atm.identity [m|ZBX_NOTSUPPORTED]`nsecond line\" }, "
        "@{ name = 'probe:atm-identity.ps1'; ok = $true; detail = 'hash matches' }) }; "
        "$m = Get-VerificationFailureMessage $r; "
        "if ($m -notlike '*key:atm.identity*') { Write-Error ('missing failing check: ' + $m); exit 1 }; "
        "if ($m -like '*probe:atm-identity.ps1*') { Write-Error ('included passing check: ' + $m); exit 1 }; "
        "if ($m.Contains(\"`n\")) { Write-Error 'detail was not collapsed to one line'; exit 1 }"
    ) % module
    proc = subprocess.run([PWSH, '-NoProfile', '-Command', code],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_valid_profile_dry_run_plans_without_writing(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-DryRun')
    assert code == 0, output
    result = _result(output)
    assert result['ok'] is True
    assert result['dry_run'] is True
    assert result['changed'] is True
    assert result['atm_id'] == 'ATM-INSTALLER-NCR'
    assert not monitor.exists() or not any(monitor.rglob('atm-agent.conf'))


def test_apply_writes_the_profiles_exact_bytes_and_records_state(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor))
    assert code == 0, output

    for entry in profile['files']:
        dest = pathlib.Path(entry['dest'])
        assert dest.is_file(), dest
        expected = hashlib.sha256(entry['content'].encode('utf-8')).hexdigest()
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == expected

    for probe in profile['probes']:
        dest = pathlib.Path(probe['dest'])
        assert dest.is_file(), dest
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == probe['sha256']

    conf = pathlib.Path(_common(monitor)[5])
    assert conf.read_text(encoding='utf-8') == profile['agent']['config_text']
    assert (monitor / 'logs').is_dir()

    state = json.loads((monitor / 'atm-agent-state.json').read_text())
    assert state['atm_id'] == 'ATM-INSTALLER-NCR'
    assert state['profile_hash'] == profile['profile_hash']
    assert state['deploy_hash'] == profile['deploy_hash']
    assert str(monitor / 'atm-agent.conf') in state['managed_files']


def test_a_second_apply_is_a_no_op_and_makes_no_backup(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    first, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                         *_common(monitor))
    assert first == 0, output
    second, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                          *_common(monitor))
    assert second == 0, output
    result = _result(output)
    assert result['changed'] is False
    assert result['backup'] in (None, '')
    assert not (monitor / 'backup').exists()


def test_unsigned_profile_is_rejected_unless_the_lab_switch_is_set(tmp_path):
    profiles, monitor = _profiles(tmp_path, unsigned=True)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-DryRun')
    assert code == 14, output
    assert 'unsigned' in _result(output)['error']
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-DryRun', '-AllowUnsignedProfile')
    assert code == 0, output


def test_not_ready_profile_is_rejected_unless_the_lab_switch_is_set(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    code, output = _run('-Profile', str(profiles / 'unready.profile.json'),
                        *_common(monitor), '-DryRun')
    assert code == 11, output
    code, output = _run('-Profile', str(profiles / 'unready.profile.json'),
                        *_common(monitor), '-DryRun', '-AllowNotReady')
    assert code == 0, output


def test_a_known_profile_hash_mismatch_is_rejected(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-DryRun',
                        '-ExpectedProfileSha256', '0' * 64)
    assert code == 12, output
    assert 'SHA-256 mismatch' in _result(output)['error']


def test_the_wrong_atm_id_is_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-DryRun',
                        '-AcceptAtmId', 'ATM-SOMEONE-ELSE')
    assert code == 15, output


def test_a_tampered_bundled_probe_is_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    tampered = tmp_path / 'tampered'
    shutil.copytree(REPO / 'scripts', tampered)
    with (tampered / 'atm-wmi-collector.ps1').open('a', encoding='utf-8') as fh:
        fh.write('\n# tampered\n')
    common = _common(monitor)
    common[common.index('-ProbeRoot') + 1] = str(tampered)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *common, '-DryRun')
    assert code == 13, output
    assert 'does not match the profile' in _result(output)['error']


def test_a_path_outside_the_monitor_root_is_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['files'][0]['dest'] = str(monitor / '..' / 'escape.conf')
    bad = profiles / 'traversal.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'outside' in _result(output)['error']


def test_approved_roots_without_a_roots_file_are_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['files'] = [entry for entry in profile['files']
                        if not entry['dest'].endswith('atm-image-roots.txt')]
    bad = profiles / 'no-roots-file.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'no roots file' in _result(output)['error']


def test_serveractive_is_refused_even_in_a_well_formed_profile(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['agent']['config_text'] = profile['agent']['config_text'].replace(
        'Server=', 'ServerActive=10.0.0.1\nServer=', 1)
    bad = profiles / 'active.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'ServerActive' in _result(output)['error']


def test_userparameters_in_the_main_config_are_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['agent']['config_text'] += (
        'UserParameter=atm.evil,powershell -File C:\\evil.ps1\n')
    bad = profiles / 'evil-userparameter.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'UserParameter' in _result(output)['error']


def test_agent1_only_remote_command_directives_are_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['agent']['config_text'] += 'EnableRemoteCommands=0\n'
    bad = profiles / 'agent1-directive.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'EnableRemoteCommands' in _result(output)['error']


def test_an_allowkey_that_permits_system_run_is_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    profile['agent']['config_text'] += 'AllowKey=system.run[*]\n'
    bad = profiles / 'allowkey.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'AllowKey=system.run' in _result(output)['error']


def test_unknown_userparameter_keys_in_the_include_are_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    include = next(entry for entry in profile['files']
                   if entry['dest'].endswith('atm-agent.conf'))
    include['content'] += (
        'UserParameter=atm.evil,powershell -File C:\\evil.ps1\n')
    bad = profiles / 'evil-include.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'unknown UserParameter' in _result(output)['error']


def test_a_second_managed_file_with_the_same_name_is_refused(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    profile = json.loads((profiles / 'ncr.profile.json').read_text())
    include = next(entry for entry in profile['files']
                   if entry['dest'].endswith('atm-agent.conf'))
    duplicate = dict(include)
    duplicate['dest'] = str(monitor / 'nested' / 'atm-agent.conf')
    profile['files'].append(duplicate)
    bad = profiles / 'duplicate-include.profile.json'
    bad.write_text(json.dumps(profile), encoding='utf-8')
    code, output = _run('-Profile', str(bad), *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'more than one atm-agent.conf' in _result(output)['error']


def test_a_profile_for_another_atm_cannot_take_over_a_managed_root(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    first, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                         *_common(monitor))
    assert first == 0, output
    code, output = _run('-Profile', str(profiles / 'noroots.profile.json'),
                        *_common(monitor), '-DryRun')
    assert code == 10, output
    assert 'already managed' in _result(output)['error']
    code, output = _run('-Profile', str(profiles / 'noroots.profile.json'),
                        *_common(monitor), '-DryRun', '-Reassign')
    assert code == 0, output


def test_removing_every_approved_root_removes_the_managed_roots_file(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    first, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                         *_common(monitor))
    assert first == 0, output
    roots = monitor / 'atm-image-roots.txt'
    assert roots.is_file()
    second, output = _run('-Profile', str(profiles / 'ncr-noroots.profile.json'),
                          *_common(monitor))
    assert second == 0, output
    assert not roots.exists()
    include = (monitor / 'atm-agent.conf').read_text(encoding='utf-8')
    assert 'atm.image' not in include


def test_verify_only_reports_a_missing_agent_instead_of_guessing(tmp_path):
    profiles, monitor = _profiles(tmp_path)
    code, output = _run('-Profile', str(profiles / 'ncr.profile.json'),
                        *_common(monitor), '-VerifyOnly',
                        '-Out', str(tmp_path / 'verify.json'))
    # The files were never applied, so the hash checks and the agent check
    # both fail; the important part is that the result is structured and the
    # caller gets the verification exit code, not a crash.
    assert code == 25, output
    result = json.loads((tmp_path / 'verify.json').read_text())
    assert result['ok'] is False
    assert any(not check['ok'] for check in result['checks'])
