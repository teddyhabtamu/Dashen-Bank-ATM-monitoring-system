import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from check_jobs import classify_tail, failure_reason  # noqa: E402


def test_grg_missing_journal_is_missing_evidence_not_a_missing_command():
    reason = failure_reason(
        'ZBX_NOTSUPPORTED: Cannot open file: [2] No such file or directory')
    assert reason == 'missing evidence file'
    assert classify_tail(
        'ZBX_NOTSUPPORTED: Cannot open file: [2] No such file or directory'
    ) == ('WARN', 'missing evidence file')


def test_real_shell_command_failure_is_still_reported():
    text = '/bin/sh: line 1: venv/bin/python: No such file or directory'
    assert failure_reason(text) == 'command not found'
    assert classify_tail(text) == ('FAILED', 'command not found')


def test_clean_log_has_no_failure_reason():
    assert failure_reason('grg-ej: 1 target(s)\n  ok: fetched') is None
    assert classify_tail('grg-ej: 1 target(s)\n  ok: fetched') == ('ok', None)
