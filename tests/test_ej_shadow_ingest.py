"""Tests for the quarantined EJ staging loader.

The loader must never turn an unknown/no-receipt session into a transaction,
and its SQL must be idempotent. These tests exercise the pure stages without a
database.
"""
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'tests'))

import ingest_ej_shadow as ing  # noqa: E402
from test_ej_journal import (  # noqa: E402
    GRG_PIPE, MAP, NCR_APPROVED, NCR_DECLINED,
)


def test_journal_meta_comes_from_the_puller_filename(tmp_path):
    path = tmp_path / '2026-10-08_TESTMACHINE_EJDATA.log'
    assert ing._journal_meta(path) == (date(2026, 10, 8), 'TESTMACHINE')
    assert ing._journal_meta(tmp_path / 'not-a-journal.log') == (None, None)


def test_empty_journal_is_still_mapped_by_hostname(tmp_path):
    path = tmp_path / '2026-10-20_TESTMACHINE_EJDATA.log'
    path.write_text('POWER-UP/RESET\n')
    runs, txns, files, errors = ing.collect(
        [path], {}, {'TESTMACHINE': 'ATM-NCR-1'})
    assert not errors
    assert runs[0]['atm_id'] == 'ATM-NCR-1'
    assert runs[0]['hostname'] == 'TESTMACHINE'
    assert runs[0]['journal_date'] == '2026-10-20'
    assert txns == []


def test_collect_stages_only_completed_mapped_transactions(tmp_path):
    journal = tmp_path / '2026-10-08_TESTMACHINE_EJDATA.log'
    journal.write_text(NCR_APPROVED + NCR_DECLINED)
    runs, txns, files, errors = ing.collect([journal], MAP)
    assert not errors
    assert len(runs) == 1 and len(txns) == 2
    assert files[0]['sessions'] == 2
    assert files[0]['staged'] == 2
    assert {t['status'] for t in txns} == {'APPROVED', 'DECLINED'}
    assert all(t['atm_id'] == 'ATM-NCR-1' for t in txns)
    assert all(t['source_file'] == str(journal) for t in txns)


def test_grg_journal_maps_transactions_by_hostname(tmp_path):
    journal = tmp_path / '2026-10-24_TESTGRG_EJDATA.log'
    journal.write_text(GRG_PIPE)
    runs, txns, files, errors = ing.collect(
        [journal], {}, {'TESTGRG': 'ATM-TEST-GRG'})
    assert not errors
    assert runs[0]['atm_id'] == 'ATM-TEST-GRG'
    assert len(txns) == 1
    assert txns[0]['atm_id'] == 'ATM-TEST-GRG'
    assert txns[0]['vendor'] == 'GRG'
    assert txns[0]['amount'] == 200.00


def test_collect_does_not_stage_a_badly_named_empty_file(tmp_path):
    journal = tmp_path / 'unknown.log'
    journal.write_text('POWER-UP/RESET\n')
    runs, txns, files, errors = ing.collect([journal], MAP)
    assert not errors
    assert runs[0]['status'] == 'error'
    assert txns == []
    assert files[0]['vendor'] == 'UNKNOWN'
    assert files[0]['staged'] == 0


def test_csv_rows_quotes_commas_and_newlines():
    text = ing._csv_rows(('a', 'b'), [{'a': 'x,y', 'b': 'line1\nline2'}])
    assert '"x,y"' in text
    assert '"line1\nline2"' in text


def test_build_sql_is_idempotent_and_never_targets_atm_transactions():
    runs, txns, _, _ = ing.collect([], MAP)
    assert runs == [] and txns == []
    sql = ing._build_sql([{
        'file_path': '/tmp/a.log', 'file_sha256': '0' * 64, 'vendor': 'NCR',
        'encoding': 'utf-8', 'sessions': 1, 'completed': 1, 'approved': 1,
        'declined': 0, 'unknown': 0, 'incomplete': 0,
        'amount_total': '200.00', 'dispensed_total': '0.00',
        'warnings': {}, 'parser_version': 'ej-journal-1', 'status': 'ok',
        'error': '', 'atm_id': 'ATM-TEST-NCR', 'hostname': 'TESTMACHINE',
        'journal_date': '2026-10-16',
    }], [{
        'transaction_key': 'EJ:NCR:PATMT003:20261016:1026',
        'parse_file': '/tmp/a.log', 'parse_sha': '0' * 64,
        'vendor': 'NCR', 'atm_id': 'ATM-TEST-NCR', 'terminal_id': 'PATMT003',
        'branch': 'TEST', 'recorded_at': '2026-10-16 11:20:00',
        'txn_type': 'WITHDRAWAL', 'status': 'APPROVED', 'amount': '200.00',
        'currency': 'ETB', 'dispensed_amount': '0.00',
        'card_masked': '923140******5264', 'auth_code': '123456',
        'error_code': '', 'seq_number': '1026', 'warnings': ['cash_not_taken'],
        'confidence': 'high', 'parser_version': 'ej-journal-1',
        'raw_ref': '/tmp/a.log', 'source_file': '/tmp/a.log',
    }])
    assert 'ON CONFLICT (transaction_key) DO UPDATE' in sql
    assert 'amount_total text' in sql, (
        'parse-run staging must be text: COPY into a numeric temp column '
        'fails on an empty value before NULLIF can run')
    assert 'journal_date' in sql and 'hostname' in sql
    assert 'ON CONFLICT (file_path, file_sha256) DO UPDATE' in sql
    assert 'INSERT INTO public.atm_transactions' not in sql
    assert 'INSERT INTO public.ej_transactions' in sql
    assert 'cash_not_taken' in sql


def test_psql_uses_the_resolved_docker_binary(monkeypatch):
    # Regression: the command list used the _docker_bin function instead of
    # the resolved string, so every DB call died in subprocess with a
    # "expected str ... not function" TypeError.
    import subprocess
    seen = {}

    def fake_run(cmd, **kwargs):
        seen['cmd'] = cmd
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(ing.subprocess, 'run', fake_run)
    ing._psql('/usr/bin/docker', 'portal', 'SELECT 1;')
    assert seen['cmd'][0] == '/usr/bin/docker'
    assert seen['cmd'][1] == 'exec'
