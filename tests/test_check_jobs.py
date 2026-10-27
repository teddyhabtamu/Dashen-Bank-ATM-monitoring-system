import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from check_jobs import failure_reason  # noqa: E402


def test_grg_missing_journal_is_not_reported_as_a_missing_command():
    reason = failure_reason(
        'ZBX_NOTSUPPORTED: Cannot open file: [2] No such file or directory')
    assert reason == 'missing file/path'


def test_real_shell_command_failure_is_still_reported():
    assert failure_reason('grg-ej: zabbix_get: command not found') == \
        'command not found'


def test_clean_log_has_no_failure_reason():
    assert failure_reason('grg-ej: 1 target(s)\n  ok: fetched') is None
