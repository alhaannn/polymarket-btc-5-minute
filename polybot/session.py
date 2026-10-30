"""
Market session and clock manager for Polymarket 5-minute (300-second) markets.
Enforces strict phase boundaries:
- WAITING: 0 <= elapsed < 180s (observation only, NO trades)
- ACTIVE:  180 <= elapsed < 300s (strategy evaluation & paper trades allowed)
- CLOSED:  elapsed >= 300s (no new trades allowed)
Uses monotonic clock references to prevent wall-clock adjustments from altering durations.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import Optional


class MarketPhase(str, enum.Enum):
    WAITING = "WAITING"    # 0 <= elapsed < 180s
    ACTIVE = "ACTIVE"      # 180 <= elapsed < 300s
    CLOSED = "CLOSED"      # elapsed >= 300s


@dataclass
class MarketTimeState:
    window_start_ts: float
    window_end_ts: float
    elapsed_sec: float
    remaining_sec: float
    phase: MarketPhase
    is_trading_allowed: bool


class MarketSessionManager:
    """
    Manages 300-second market window cycles.
    Anchors elapsed time against time.monotonic() to prevent wall clock jumps.
    """

    WINDOW_DURATION_SEC: float = 300.0
    WAITING_PERIOD_SEC: float = 180.0

    def __init__(self, window_duration_sec: float = 300.0, waiting_period_sec: float = 180.0):
        self.window_duration_sec = window_duration_sec
        self.waiting_period_sec = waiting_period_sec

        self.current_window_start: int = 0
        self._anchor_wall_ts: float = 0.0
        self._anchor_mono_ts: float = 0.0

    def sync(self, wall_ts: Optional[float] = None) -> MarketTimeState:
        """
        Synchronize the session clock.
        If a new 300-second epoch is reached, re-anchors to monotonic clock.
        """
        now_wall = wall_ts if wall_ts is not None else time.time()
        now_mono = time.monotonic()

        window_start = int(now_wall // self.window_duration_sec) * int(self.window_duration_sec)

        if self.current_window_start != window_start or self._anchor_wall_ts == 0.0:
            self.current_window_start = window_start
            self._anchor_wall_ts = now_wall
            self._anchor_mono_ts = now_mono

        # Measure elapsed time using monotonic offset from anchor plus initial offset
        initial_wall_offset = max(0.0, self._anchor_wall_ts - window_start)
        mono_offset = max(0.0, now_mono - self._anchor_mono_ts)
        elapsed = initial_wall_offset + mono_offset

        # Fallback safeguard: if elapsed differs widely from wall calculation, clamp to wall
        direct_wall_elapsed = now_wall - window_start
        if abs(elapsed - direct_wall_elapsed) > 2.0:
            elapsed = direct_wall_elapsed

        elapsed = max(0.0, elapsed)
        remaining = max(0.0, self.window_duration_sec - elapsed)

        if elapsed < self.waiting_period_sec:
            phase = MarketPhase.WAITING
            trading_allowed = False
        elif elapsed < self.window_duration_sec:
            phase = MarketPhase.ACTIVE
            trading_allowed = True
        else:
            phase = MarketPhase.CLOSED
            trading_allowed = False

        return MarketTimeState(
            window_start_ts=float(window_start),
            window_end_ts=float(window_start + self.window_duration_sec),
            elapsed_sec=round(elapsed, 3),
            remaining_sec=round(remaining, 3),
            phase=phase,
            is_trading_allowed=trading_allowed,
        )

    def get_state_for_time(self, wall_ts: float, target_window_start: Optional[int] = None) -> MarketTimeState:
        """Deterministic state evaluation for backtesting or specific timestamps."""
        if target_window_start is not None:
            window_start = target_window_start
        else:
            window_start = int(wall_ts // self.window_duration_sec) * int(self.window_duration_sec)

        elapsed = max(0.0, wall_ts - window_start)
        remaining = max(0.0, self.window_duration_sec - elapsed)

        if elapsed < self.waiting_period_sec:
            phase = MarketPhase.WAITING
            trading_allowed = False
        elif elapsed < self.window_duration_sec:
            phase = MarketPhase.ACTIVE
            trading_allowed = True
        else:
            phase = MarketPhase.CLOSED
            trading_allowed = False

        return MarketTimeState(
            window_start_ts=float(window_start),
            window_end_ts=float(window_start + self.window_duration_sec),
            elapsed_sec=round(elapsed, 3),
            remaining_sec=round(remaining, 3),
            phase=phase,
            is_trading_allowed=trading_allowed,
        )
