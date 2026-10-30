"""Reporting windows: custom EAT dates or the legacy relative day count."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from anomaly_dates import eat_day_end_utc, eat_day_start_utc

EAT = timezone(timedelta(hours=3))
MAX_REPORT_DAYS = 90


@dataclass(frozen=True)
class ReportWindow:
    start_utc: datetime
    end_utc: datetime
    days: int
    label: str


@dataclass(frozen=True)
class ScheduleWindow:
    start: str
    end: str | None
    end_now: bool
    label: str


SCHEDULE_PERIODS = ('today', 'yesterday', 'this_week', 'last_week',
                    'this_month', 'last_month')


def schedule_period_window(period, today=None):
    if period not in SCHEDULE_PERIODS:
        raise ValueError('Unknown scheduled reporting period.')
    if today is None:
        today = _eat_today()
    elif isinstance(today, datetime):
        today = today.date()

    if period == 'today':
        return ScheduleWindow(today.isoformat(), None, True, 'Today (to now)')
    if period == 'yesterday':
        day = today - timedelta(days=1)
        return ScheduleWindow(day.isoformat(), day.isoformat(), False, 'Yesterday')
    if period == 'this_week':
        start = today - timedelta(days=today.weekday())
        return ScheduleWindow(start.isoformat(), None, True, 'This week (to now)')
    if period == 'last_week':
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
        return ScheduleWindow(start.isoformat(), end.isoformat(), False, 'Last week')
    if period == 'this_month':
        start = today.replace(day=1)
        return ScheduleWindow(start.isoformat(), None, True, 'This month (to now)')

    last_day = today.replace(day=1) - timedelta(days=1)
    start = last_day.replace(day=1)
    return ScheduleWindow(start.isoformat(), last_day.isoformat(), False, 'Last month')


def _eat_today(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(EAT).date()
    return now


def _now_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError('Invalid clock.')


def parse_report_window(start=None, end=None, days=None, now=None, end_now=False):
    today = _eat_today(now)
    start = str(start or '').strip()
    end = str(end or '').strip()

    if end_now:
        if not start:
            raise ValueError('Choose a start date.')
        try:
            start_date = date.fromisoformat(start)
        except ValueError:
            raise ValueError('Dates must use YYYY-MM-DD.')
        if start_date > today:
            raise ValueError('Start date cannot be in the future.')
        span = (today - start_date).days + 1
        if span > MAX_REPORT_DAYS:
            raise ValueError(f'Reporting period cannot exceed {MAX_REPORT_DAYS} days.')
        start_utc = eat_day_start_utc(start_date.isoformat())
        end_utc = _now_utc(now)
        if end_utc < start_utc:
            raise ValueError('Reporting period is empty.')
        label = (
            f'{start_date.isoformat()} (EAT, to now)'
            if start_date == today else
            f'{start_date.isoformat()} to {today.isoformat()} (EAT, to now)'
        )
        return ReportWindow(start_utc, end_utc, span, label)

    if start or end:
        if not start or not end:
            raise ValueError('Choose both a start date and an end date.')
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError:
            raise ValueError('Dates must use YYYY-MM-DD.')
        if start_date > end_date:
            raise ValueError('Start date must be on or before the end date.')
        if end_date > today:
            raise ValueError('End date cannot be in the future.')
        span = (end_date - start_date).days + 1
        if span > MAX_REPORT_DAYS:
            raise ValueError(f'Reporting period cannot exceed {MAX_REPORT_DAYS} days.')
        return ReportWindow(
            eat_day_start_utc(start_date.isoformat()),
            eat_day_end_utc(end_date.isoformat()),
            span,
            f'{start_date.isoformat()} to {end_date.isoformat()} (EAT)',
        )

    try:
        count = int(days) if days not in (None, '') else 7
    except (TypeError, ValueError):
        raise ValueError('Reporting period must be a whole number of days.')
    if count < 1:
        raise ValueError('Reporting period must be at least one day.')
    if count > MAX_REPORT_DAYS:
        raise ValueError(f'Reporting period cannot exceed {MAX_REPORT_DAYS} days.')

    end_utc = _now_utc(now)
    start_utc = end_utc - timedelta(days=count)
    return ReportWindow(
        start_utc,
        end_utc,
        count,
        f'Last {count} day{"s" if count != 1 else ""}',
    )
