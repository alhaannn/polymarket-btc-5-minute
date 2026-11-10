"""
Timezone utilities for Indian Standard Time (IST, UTC+05:30).
Ensures uniform time displays, log timestamps, and trading day attribution.
"""
from __future__ import annotations

import datetime
import os
from typing import Any, Dict, List, Optional

# Indian Standard Time (UTC+05:30)
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")


def format_duration(seconds: float) -> str:
    """Formats duration in seconds to a human-readable string (e.g. '1h 24m 10s', '24m 10s', '45s')."""
    sec = max(0, int(round(seconds)))
    hours = sec // 3600
    mins = (sec % 3600) // 60
    secs = sec % 60
    if hours > 0:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    elif mins > 0:
        return f"{mins}m {secs:02d}s"
    else:
        return f"{secs}s"


def compute_schedule_state(
    prime_hours_only: bool = True,
    shutdown_active: bool = False,
    shutdown_until_ts: Optional[float] = None,
    trades: Optional[List[Dict[str, Any]]] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes current operating schedule status (WORKING vs RESTING),
    countdown to work start or working runtime, and session PnL.
    Prime hours schedule: 13:30 IST to 01:00 IST daily.
    """
    now_dt = to_ist(ts)
    now_ts = now_dt.timestamp()
    h = now_dt.hour
    m = now_dt.minute
    s = now_dt.second

    # Prime hours: 13:30 to 01:00 IST
    in_prime_time = (h > 13) or (h == 13 and m >= 30) or (h == 0) or (h == 1 and m == 0 and s == 0)

    if not prime_hours_only:
        # 24/7 continuous mode
        session_start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        session_end_dt = session_start_dt + datetime.timedelta(days=1)
        next_start_dt = now_dt
        in_schedule = True
    else:
        in_schedule = in_prime_time
        if in_prime_time:
            if h >= 13:
                session_start_dt = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)
                session_end_dt = (now_dt + datetime.timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
            else:
                session_start_dt = (now_dt - datetime.timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
                session_end_dt = now_dt.replace(hour=1, minute=0, second=0, microsecond=0)
            next_start_dt = session_start_dt + datetime.timedelta(days=1)
        else:
            # Resting outside prime hours
            if h < 13 or (h == 13 and m < 30):
                next_start_dt = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)
                session_start_dt = (now_dt - datetime.timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
                session_end_dt = now_dt.replace(hour=1, minute=0, second=0, microsecond=0)
            else:
                next_start_dt = (now_dt + datetime.timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
                session_start_dt = now_dt.replace(hour=13, minute=30, second=0, microsecond=0)
                session_end_dt = (now_dt + datetime.timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)

    # Daily shutdown / risk circuit-breaker override
    if os.getenv("DISABLE_DAILY_SHUTDOWN", "false").lower() in ("true", "1", "yes"):
        is_shutdown = False
    else:
        is_shutdown = shutdown_active or (shutdown_until_ts is not None and shutdown_until_ts > now_ts)

    if is_shutdown:
        status = "RESTING"
        is_working = False
        phase = "DAILY_SHUTDOWN"
        reason = "Daily Shutdown / Risk Cooldown Active"
        time_left_to_start_sec = max(0.0, (shutdown_until_ts or now_ts) - now_ts)
        working_elapsed_sec = 0.0
        working_remaining_sec = 0.0
    elif in_schedule:
        status = "WORKING"
        is_working = True
        phase = "PRIME_HOURS" if prime_hours_only else "24_7_ACTIVE"
        reason = "Prime Hours Active (High Liquidity)" if prime_hours_only else "24/7 Trading Mode Active"
        time_left_to_start_sec = 0.0
        working_elapsed_sec = max(0.0, (now_dt - session_start_dt).total_seconds())
        working_remaining_sec = max(0.0, (session_end_dt - now_dt).total_seconds())
    else:
        status = "RESTING"
        is_working = False
        phase = "OFF_HOURS"
        reason = "Off-Hours Resting (Shielding from spread spikes & low liquidity)"
        time_left_to_start_sec = max(0.0, (next_start_dt - now_dt).total_seconds())
        working_elapsed_sec = 0.0
        working_remaining_sec = 0.0

    # Calculate Session PnL & Trade Metrics
    session_start_ts = session_start_dt.timestamp()
    session_pnl_usd = 0.0
    session_trades_count = 0
    session_wins = 0
    session_losses = 0

    if trades:
        for t in trades:
            exec_ts = t.get("exec_ts") or 0.0
            if exec_ts >= session_start_ts:
                session_trades_count += 1
                if t.get("status") == "SETTLED" and t.get("pnl_usd") is not None:
                    pnl = float(t["pnl_usd"])
                    session_pnl_usd += pnl
                    if pnl > 0:
                        session_wins += 1
                    elif pnl < 0:
                        session_losses += 1

    session_pnl_usd = round(session_pnl_usd, 2)

    return {
        "status": status,
        "is_working": is_working,
        "phase": phase,
        "reason": reason,
        "prime_hours_only": prime_hours_only,
        "schedule_window": "13:30 – 01:00 IST",
        "current_time_ist": ist_time_str(now_ts),
        "session_start_ts": session_start_ts,
        "session_start_str": ist_time_str(session_start_ts),
        "session_end_ts": session_end_dt.timestamp(),
        "session_end_str": ist_time_str(session_end_dt.timestamp()),
        "next_start_ts": next_start_dt.timestamp(),
        "next_start_str": ist_time_str(next_start_dt.timestamp()),
        "time_left_to_start_sec": round(time_left_to_start_sec, 1),
        "time_left_to_start_formatted": format_duration(time_left_to_start_sec),
        "working_elapsed_sec": round(working_elapsed_sec, 1),
        "working_elapsed_formatted": format_duration(working_elapsed_sec),
        "working_remaining_sec": round(working_remaining_sec, 1),
        "working_remaining_formatted": format_duration(working_remaining_sec),
        "session_pnl_usd": session_pnl_usd,
        "session_trades_count": session_trades_count,
        "session_wins": session_wins,
        "session_losses": session_losses,
    }


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
