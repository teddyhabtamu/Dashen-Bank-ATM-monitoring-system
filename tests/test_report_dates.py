"""Report windows: custom EAT dates with a safe legacy day fallback."""
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from report_dates import MAX_REPORT_DAYS, parse_report_window  # noqa: E402


def test_absolute_range_converts_eat_boundaries_to_utc():
    win = parse_report_window('2026-10-01', '2026-10-15',
                              now=datetime(2026, 10, 21, 12))
    assert win.start_utc == datetime(2026, 9, 30, 21, 0)
    assert win.end_utc == datetime(2026, 10, 15, 21, 0)
    assert win.days == 15
    assert win.label == '2026-10-01 to 2026-10-15 (EAT)'


def test_single_day_is_inclusive_and_24_hours():
    win = parse_report_window('2026-10-21', '2026-10-21',
                              now=datetime(2026, 10, 21, 12))
    assert win.days == 1
    assert win.end_utc - win.start_utc == timedelta(hours=24)


def test_start_after_end_is_rejected():
    with pytest.raises(ValueError):
        parse_report_window('2026-10-15', '2026-10-01',
                            now=datetime(2026, 10, 21, 12))


def test_future_end_is_rejected_in_eat():
    with pytest.raises(ValueError):
        parse_report_window('2026-10-21', '2026-10-22',
                            now=datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc))


def test_only_one_date_is_rejected():
    with pytest.raises(ValueError):
        parse_report_window('2026-10-01', None, now=datetime(2026, 10, 21, 12))
    with pytest.raises(ValueError):
        parse_report_window(None, '2026-10-01', now=datetime(2026, 10, 21, 12))


def test_invalid_calendar_dates_are_rejected():
    for start, end in (('10/01/2026', '2026-10-02'),
                       ('2026-02-30', '2026-03-01'),
                       ('2026-10-01', '2026-13-01')):
        with pytest.raises(ValueError):
            parse_report_window(start, end, now=datetime(2026, 10, 21, 12))


def test_span_over_the_limit_is_rejected():
    with pytest.raises(ValueError):
        parse_report_window('2026-01-01', '2026-04-01',
                            now=datetime(2026, 10, 21, 12))


def test_legacy_days_still_work_for_the_scheduler():
    win = parse_report_window(days='7', now=datetime(2026, 10, 21, 12))
    assert win.days == 7
    assert win.end_utc == datetime(2026, 10, 21, 12)
    assert win.start_utc == datetime(2026, 10, 14, 12)
    assert win.label == 'Last 7 days'


def test_legacy_single_day_label():
    assert parse_report_window(days='1', now=datetime(2026, 10, 21, 12)).label \
        == 'Last 1 day'


def test_legacy_days_default_to_seven():
    win = parse_report_window(now=datetime(2026, 10, 21, 12))
    assert win.days == 7


def test_legacy_days_are_capped_and_validated():
    with pytest.raises(ValueError):
        parse_report_window(days=str(MAX_REPORT_DAYS + 1),
                            now=datetime(2026, 10, 21, 12))
    with pytest.raises(ValueError):
        parse_report_window(days='0', now=datetime(2026, 10, 21, 12))
    with pytest.raises(ValueError):
        parse_report_window(days='abc', now=datetime(2026, 10, 21, 12))
