import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from state_summary import merge_live_in_service  # noqa: E402


def _atm(stored='IN_SERVICE', live='IN_SERVICE', status='active', has_live=True):
    return {
        'stored_state': stored,
        'state': live,
        'status': status,
        'live': has_live,
    }


def test_live_outage_replaces_stored_in_service():
    assert merge_live_in_service(1, [_atm(live='OUT_OF_SERVICE')]) == 0


def test_live_recovery_counts_even_if_stored_was_unknown():
    assert merge_live_in_service(0, [_atm(stored='UNKNOWN')]) == 1


def test_rows_without_live_data_keep_the_stored_count():
    assert merge_live_in_service(1, [_atm(live='OUT_OF_SERVICE', has_live=False)]) == 1


def test_inactive_rows_are_not_counted():
    assert merge_live_in_service(0, [_atm(status='inactive')]) == 0


def test_missing_status_defaults_to_active():
    row = _atm(stored='UNKNOWN')
    row.pop('status')
    assert merge_live_in_service(0, [row]) == 1


def test_unchanged_state_does_not_double_count():
    assert merge_live_in_service(1, [_atm()]) == 1


def test_count_never_goes_negative():
    assert merge_live_in_service(0, [_atm(live='OUT_OF_SERVICE')]) == 0
