"""EAT calendar-day boundaries expressed as naive UTC for DB filters."""
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3))


def eat_day_start_utc(value):
    day = datetime.strptime(value, '%Y-%m-%d').date()
    return (datetime(day.year, day.month, day.day, tzinfo=EAT)
            .astimezone(timezone.utc).replace(tzinfo=None))


def eat_day_end_utc(value):
    day = datetime.strptime(value, '%Y-%m-%d').date() + timedelta(days=1)
    return (datetime(day.year, day.month, day.day, tzinfo=EAT)
            .astimezone(timezone.utc).replace(tzinfo=None))
