"""Date/time helpers. All customer-facing times are in the venue timezone."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

LOCAL_TZ = ZoneInfo(settings.DEFAULT_TIMEZONE)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_date_today() -> date:
    return now_local().date()


def combine_local(date_str: str | None, time_str: str | None) -> datetime | None:
    """
    Combine a 'YYYY-MM-DD' date and a 'HH:MM' time (local) into an aware datetime.

    Returns None if either part is missing or unparseable.
    """
    if not date_str or not time_str:
        return None
    date_str = date_str.strip()
    time_str = time_str.strip()
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return None
    # Accept 'HH:MM' or 'HH:MM:SS'.
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            t = None
    if t is None:
        return None
    return datetime.combine(d, t, tzinfo=LOCAL_TZ)


def add_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
