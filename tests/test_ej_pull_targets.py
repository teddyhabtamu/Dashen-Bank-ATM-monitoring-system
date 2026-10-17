"""Unit tests for the EJ pull target resolver.

The resolver decides which ATMs get their journals pulled every hour, so a
silent bug here either stops data collection or points it at the wrong box.
Both failure modes are covered below.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import ej_pull_targets as ept  # noqa: E402

OK_ROW = {'atm_id': 'ATM-0001', 'vendor': 'NCR', 'status': 'active',
          'ip_address': '10.41.30.114', 'ej_hostname': 'TESTMACHINE'}


def test_ncr_active_with_hostname_is_a_target():
    targets, skips = ept.targets_from_rows([OK_ROW])
    assert targets == [('ATM-0001', '10.41.30.114', 'TESTMACHINE')]
    assert skips == []


def test_grg_has_no_journal_pull_path():
    row = dict(OK_ROW, vendor='GRG')
    targets, skips = ept.targets_from_rows([row])
    assert targets == []
    assert 'journal pull' in skips[0][1]


def test_inactive_atm_is_skipped():
    targets, skips = ept.targets_from_rows([dict(OK_ROW, status='inactive')])
    assert targets == [] and 'not active' in skips[0][1]


def test_missing_status_defaults_to_active():
    row = dict(OK_ROW)
    del row['status']
    assert ept.targets_from_rows([row])[0] == [('ATM-0001', '10.41.30.114', 'TESTMACHINE')]


def test_missing_ip_is_skipped():
    targets, skips = ept.targets_from_rows([dict(OK_ROW, ip_address='')])
    assert targets == [] and 'no IP address' in skips[0][1]


def test_missing_hostname_is_skipped_not_guessed():
    """The journal filename embeds the machine hostname — never invent one."""
    targets, skips = ept.targets_from_rows([dict(OK_ROW, ej_hostname=None)])
    assert targets == [] and 'no EJ hostname' in skips[0][1]


def test_hostname_is_upper_cased_and_trimmed():
    row = dict(OK_ROW, ej_hostname='  testmachine ')
    assert ept.targets_from_rows([row])[0] == [('ATM-0001', '10.41.30.114', 'TESTMACHINE')]


def test_separators_in_fields_are_rejected():
    row = dict(OK_ROW, ej_hostname='HOST:1')
    targets, skips = ept.targets_from_rows([row])
    assert targets == [] and skips


def test_duplicates_are_collapsed():
    targets, _ = ept.targets_from_rows([OK_ROW, dict(OK_ROW)])
    assert targets == [('ATM-0001', '10.41.30.114', 'TESTMACHINE')]


def test_override_wins_over_every_source():
    os.environ['EJ_ATMS'] = 'ATM-9999:1.2.3.4:BOX9'
    try:
        targets, source, _ = ept.resolve(docker_bin='/nonexistent/docker')
        assert targets == [('ATM-9999', '1.2.3.4', 'BOX9')]
        assert source == 'EJ_ATMS override'
    finally:
        del os.environ['EJ_ATMS']


def test_malformed_override_entries_are_ignored_not_fatal():
    targets, _ = ept.from_override('ATM-1:1.2.3.4 BOX_ONLY ATM-2:1.2.3.5:BOX2')
    assert targets == [('ATM-2', '1.2.3.5', 'BOX2')]


def _fake_psql(stdout, returncode=0, stderr=''):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


def test_db_source_parses_pipe_separated_rows(monkeypatch):
    # DB_NAME must be explicit now. Left to itself the resolver detects the
    # database by asking for the portal marker, which this fake answers with
    # ATM rows, so detection finds nothing and the DB path is never reached.
    monkeypatch.setenv('DB_NAME', 'portal')
    monkeypatch.setattr(ept.subprocess, 'run',
                        _fake_psql('ATM-0001|10.41.30.114|TESTMACHINE\nATM-0002|10.41.30.115|BOX2\n'))
    targets, source, _ = ept.resolve()
    assert source.startswith('database'), source
    assert targets == [('ATM-0001', '10.41.30.114', 'TESTMACHINE'),
                       ('ATM-0002', '10.41.30.115', 'BOX2')]


def test_db_failure_falls_back_to_csv(monkeypatch, tmp_path):
    """DB unreachable must degrade to the snapshot, never to an empty list."""
    csv_file = tmp_path / 'atm_locations.csv'
    csv_file.write_text('atm_id,vendor,status,ip_address,ej_hostname\n'
                        'ATM-0007,NCR,active,10.41.30.120,BOX7\n')
    monkeypatch.setattr(ept.subprocess, 'run',
                        _fake_psql('', returncode=1, stderr='Cannot connect'))
    targets, source, _ = ept.resolve(csv_path=str(csv_file), docker_bin='docker')
    assert source == 'CSV snapshot'
    assert targets == [('ATM-0007', '10.41.30.120', 'BOX7')]


def test_db_and_csv_missing_returns_empty_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(ept.subprocess, 'run', _fake_psql('', returncode=1))
    targets, source, _ = ept.resolve(csv_path=str(tmp_path / 'nope.csv'))
    assert (targets, source) == ([], 'none')


def test_empty_but_reachable_db_falls_through_to_csv(monkeypatch, tmp_path):
    """Zero eligible rows in the DB is not the same as a broken DB."""
    csv_file = tmp_path / 'atm_locations.csv'
    csv_file.write_text('atm_id,vendor,status,ip_address,ej_hostname\n'
                        'ATM-0008,NCR,active,10.41.30.121,BOX8\n')
    monkeypatch.setattr(ept.subprocess, 'run', _fake_psql(''))
    targets, source, _ = ept.resolve(csv_path=str(csv_file))
    assert source == 'CSV snapshot' and targets[0][0] == 'ATM-0008'

def test_docker_bin_is_configurable_for_cron(monkeypatch):
    """A cron PATH without docker must be fixable without editing code."""
    monkeypatch.setenv('EJ_DOCKER_BIN', '/usr/bin/docker')
    monkeypatch.setenv('DB_NAME', 'portal')
    seen = {}

    def fake_run(cmd, **kw):
        seen['cmd'] = cmd
        return subprocess.CompletedProcess(cmd, 0, 'ATM-0001|1.2.3.4|BOX\n', '')

    monkeypatch.setattr(ept.subprocess, 'run', fake_run)
    targets, source, _ = ept.resolve()
    assert source.startswith('database'), source
    assert seen['cmd'][0] == '/usr/bin/docker'


def test_the_database_is_detected_when_db_name_is_not_set(monkeypatch):
    """The cron line exports ZBX_URL/ZBX_USER/ZBX_PASS but not DB_NAME, so
    detection is what actually runs on the per-minute path. Without it the
    resolver read the pre-split database, which returns the old rows without
    erroring - so an ATM registered in the portal would quietly stop having its
    journals pulled."""
    calls = []

    def fake_run(cmd, **kw):
        # The marker probe runs first; the data query second.
        marker = any('portal_schema_version' in str(a) for a in cmd)
        calls.append(cmd)
        if marker:
            return subprocess.CompletedProcess(cmd, 0, 't\n', '')
        return subprocess.CompletedProcess(
            cmd, 0, 'ATM-0001|10.41.30.114|TESTMACHINE\n', '')

    monkeypatch.delenv('DB_NAME', raising=False)
    monkeypatch.setattr(ept.subprocess, 'run', fake_run)

    targets, source, _ = ept.resolve()
    assert source.startswith('database'), source
    assert targets == [('ATM-0001', '10.41.30.114', 'TESTMACHINE')]
    probed = [c for c in calls if 'portal_schema_version' in str(c)]
    assert probed, 'detection must actually probe for the marker'
    # and the data query must go to the detected database, not to zabbix
    data_calls = [c for c in calls if 'portal_schema_version' not in str(c)]
    assert any('-d' in c and c[c.index('-d') + 1] == 'portal' for c in data_calls), (
        'the read must use the detected database')
    assert 'portal' in source, (
        'the log must name the database, not just say "database" - reading the '
        'pre-split one has to be visible: %s' % source)
