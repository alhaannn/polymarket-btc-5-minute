"""
Timezone utilities for Indian Standard Time (IST, UTC+05:30).
Ensures uniform time displays, log timestamps, and trading day attribution.
"""
from __future__ import annotations

import datetime
from typing import Optional

# Indian Standard Time (UTC+05:30)
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime.datetime:
    """Returns the current datetime in Indian Standard Time (IST)."""
    return datetime.datetime.now(IST_TZ)


def to_ist(ts: Optional[float] = None) -> datetime.datetime:
    """Converts a Unix timestamp (seconds) or current time to an IST datetime."""
    if ts is None:
        return now_ist()
    return datetime.datetime.fromtimestamp(ts, tz=IST_TZ)


def ist_time_str(ts: Optional[float] = None, fmt: str = "%H:%M:%S") -> str:
    """Formats timestamp into an IST time string (e.g. '13:06:40')."""
    return to_ist(ts).strftime(fmt)


def ist_datetime_str(ts: Optional[float] = None, fmt: str = "%Y-%m-%d %H:%M:%S IST") -> str:
    """Formats timestamp into an IST datetime string (e.g. '2026-11-01 13:06:40 IST')."""
    return to_ist(ts).strftime(fmt)


def ist_today_str(ts: Optional[float] = None) -> str:
    """Returns the trading day string 'YYYY-MM-DD' in IST."""
    return to_ist(ts).strftime("%Y-%m-%d")
