"""Report windows: custom EAT dates with a safe legacy day fallback."""
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'report-portal'))

from report_dates import (MAX_REPORT_DAYS, ScheduleWindow,  # noqa: E402
                          parse_report_window, schedule_period_window)


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


def test_schedule_calendar_presets():
    today = date(2026, 10, 21)  # Wednesday
    assert schedule_period_window('today', today) == \
        ScheduleWindow('2026-10-21', None, True, 'Today (to now)')
    assert schedule_period_window('yesterday', today) == \
        ScheduleWindow('2026-10-20', '2026-10-20', False, 'Yesterday')
    assert schedule_period_window('this_week', today) == \
        ScheduleWindow('2026-10-19', None, True, 'This week (to now)')
    assert schedule_period_window('last_week', today) == \
        ScheduleWindow('2026-10-12', '2026-10-18', False, 'Last week')
    assert schedule_period_window('this_month', today) == \
        ScheduleWindow('2026-10-01', None, True, 'This month (to now)')
    assert schedule_period_window('last_month', today) == \
        ScheduleWindow('2026-09-01', '2026-09-30', False, 'Last month')


def test_schedule_last_month_handles_january():
    assert schedule_period_window('last_month', date(2026, 1, 15)) == \
        ScheduleWindow('2025-12-01', '2025-12-31', False, 'Last month')


def test_schedule_rejects_unknown_period():
    with pytest.raises(ValueError):
        schedule_period_window('last_quarter', date(2026, 10, 21))


def test_end_now_uses_eat_midnight_and_current_instant():
    win = parse_report_window('2026-10-21', None, now=datetime(2026, 10, 21, 7),
                              end_now=True)
    assert win.start_utc == datetime(2026, 10, 20, 21, 0)
    assert win.end_utc == datetime(2026, 10, 21, 7, 0)
    assert win.days == 1
    assert win.label == '2026-10-21 (EAT, to now)'


def test_end_now_multi_day_span():
    win = parse_report_window('2026-10-19', None, now=datetime(2026, 10, 21, 12),
                              end_now=True)
    assert win.start_utc == datetime(2026, 10, 18, 21, 0)
    assert win.end_utc == datetime(2026, 10, 21, 12, 0)
    assert win.days == 3
    assert win.label == '2026-10-19 to 2026-10-21 (EAT, to now)'


def test_end_now_requires_a_start_date():
    with pytest.raises(ValueError):
        parse_report_window(None, None, now=datetime(2026, 10, 21, 7),
                            end_now=True)


def test_end_now_rejects_future_and_overlong_windows():
    with pytest.raises(ValueError):
        parse_report_window('2026-10-22', None, now=datetime(2026, 10, 21, 7),
                            end_now=True)
    with pytest.raises(ValueError):
        parse_report_window('2026-07-01', None, now=datetime(2026, 10, 21, 7),
                            end_now=True)
