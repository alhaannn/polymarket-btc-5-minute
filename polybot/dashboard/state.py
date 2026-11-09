"""
Shared state management for the live local web dashboard.
Exposes thread-safe/async-safe snapshots of market clock, probabilities,
strategy conditions, recent events, latency, and controls.
"""
from __future__ import annotations

import collections
import time
from typing import Any, Deque, Dict, List, Optional

from polybot.tz import ist_datetime_str, ist_time_str


class DashboardState:
    """Singleton-style or injected state holder for the local web dashboard."""

    def __init__(self):
        self.bot_status: str = "INITIALIZING"  # RUNNING, PAUSED, STOPPED, DISCONNECTED
        self.execution_mode: str = "paper"  # paper, live
        self.strategy_name: str = "reversal"

        # Market & clock
        self.current_slug: str = "waiting_for_market"
        self.window_start: int = 0
        self.window_end: int = 0
        self.elapsed_sec: float = 0.0
        self.remaining_sec: float = 300.0
        self.phase: str = "WAITING"  # WAITING, ACTIVE, CLOSED
        self.is_trading_allowed: bool = False

        # Probabilities & prices
        self.up_prob: Optional[float] = None
        self.down_prob: Optional[float] = None
        self.up_bid: Optional[float] = None
        self.down_bid: Optional[float] = None
        self.last_update_ts: float = 0.0

        # Strategy triggers
        self.initial_range_met: bool = False
        self.reversal_range_met: bool = False
        self.initial_order_triggered: bool = False
        self.initial_side: Optional[str] = None
        self.reversal_order_triggered: bool = False

        # Schedule & Working/Resting state
        self.prime_hours_only: bool = True
        self.schedule_state: Optional[Dict[str, Any]] = None

        # Feed & Latency
        self.feed_status: str = "CONNECTING"  # CONNECTED, STALE, DISCONNECTED
        self.last_tick_receipt_ts: float = 0.0
        self.last_latency_ms: float = 0.0

        # Recent events/logs
        self.logs: Deque[Dict[str, Any]] = collections.deque(maxlen=1000)

        # Capital State & Compounding (Roadmap Phase 7 Section B)
        self.capital_state: Dict[str, Any] = {
            "total_equity": 11.0,
            "active_capital": 11.0,
            "protected_capital": 0.0,
            "spendable_active_funds": 11.0,
            "active_funds_reserved": 0.0,
            "protected_funds": 0.0,
            "unsettled_funds": 0.0,
            "current_initial_budget": 1.0,
            "current_reversal_budget": 10.0,
            "realized_net_pnl": 0.0,
            "realized_profit": 0.0,
            "realized_loss": 0.0,
            "fees": 0.0,
            "open_exposure": 0.0,
            "reserved_collateral": 0.0,
            "active_shortfall": 0.0,
            "milestone_44_activated": False,
            "daily_shutdown_status": False,
            "daily_shutdown_reason": "",
            "daily_shutdown_trading_day": "",
            "daily_failed_initial_count": 0,
            "daily_failed_reversal_count": 0,
            "next_day_activation_eligible": False,
            "last_activated_shutdown_event_id": None,
            "last_activation_trading_day": None,
            "last_reserve_activation": None,
            "reconciliation_status": "OK",
            "last_reconciliation_ts": 0.0,
            "last_reconciliation_diff": 0.0,
            "reconciliation_blocked": False,
        }

        # Multi-Market 6-Asset Architecture State
        self.multi_market: Optional[Dict[str, Any]] = None

        # Control signals requested from UI
        self.requested_action: Optional[str] = None

    def add_log(self, level: str, message: str) -> None:
        now_ts = time.time()
        self.logs.appendleft({
            "timestamp": now_ts,
            "time_str": ist_time_str(now_ts),
            "level": level.upper(),
            "message": message,
        })

    def update_clock(
        self,
        slug: str,
        window_start: int,
        window_end: int,
        elapsed_sec: float,
        remaining_sec: float,
        phase: str,
        is_trading_allowed: bool,
    ) -> None:
        self.current_slug = slug
        self.window_start = window_start
        self.window_end = window_end
        self.elapsed_sec = elapsed_sec
        self.remaining_sec = remaining_sec
        self.phase = phase
        self.is_trading_allowed = is_trading_allowed

    def update_probabilities(
        self,
        up_prob: Optional[float],
        down_prob: Optional[float],
        up_bid: Optional[float] = None,
        down_bid: Optional[float] = None,
        receipt_ts: Optional[float] = None,
        latency_ms: float = 0.0,
    ) -> None:
        self.up_prob = up_prob
        self.down_prob = down_prob
        self.up_bid = up_bid
        self.down_bid = down_bid
        self.last_update_ts = time.time()
        self.last_tick_receipt_ts = receipt_ts or time.time()
        if latency_ms > 0:
            self.last_latency_ms = latency_ms

    def update_strategy_state(
        self,
        initial_met: bool,
        reversal_met: bool,
        initial_triggered: bool,
        reversal_triggered: bool,
        initial_side: Optional[str] = None,
    ) -> None:
        self.initial_range_met = initial_met
        self.reversal_range_met = reversal_met
        self.initial_order_triggered = initial_triggered
        self.reversal_order_triggered = reversal_triggered
        self.initial_side = initial_side

    def set_feed_status(self, status: str) -> None:
        self.feed_status = status

    def set_bot_status(self, status: str) -> None:
        self.bot_status = status

    def request_control(self, action: str) -> None:
        self.requested_action = action.upper()
        self.add_log("CONTROL", f"User requested action: {self.requested_action}")

    def pop_requested_action(self) -> Optional[str]:
        act = self.requested_action
        self.requested_action = None
        return act

    def update_capital(self, cap_dict: Dict[str, Any]) -> None:
        self.capital_state.update(cap_dict)

    def update_schedule_state(self, sched_dict: Dict[str, Any]) -> None:
        self.schedule_state = sched_dict

    def update_multi_market(self, matrix_dict: Dict[str, Any]) -> None:
        self.multi_market = matrix_dict

    def to_dict(self) -> Dict[str, Any]:
        """Snapshot of current system state for API/UI."""
        now = time.time()
        staleness = round(now - self.last_update_ts, 2) if self.last_update_ts > 0 else 999.0
        feed_quality = "CONNECTED"
        if staleness > 5.0:
            feed_quality = "STALE"
        if self.feed_status == "DISCONNECTED":
            feed_quality = "DISCONNECTED"

        sched = self.schedule_state
        if sched is None:
            from polybot.tz import compute_schedule_state
            sched = compute_schedule_state(prime_hours_only=self.prime_hours_only, ts=now)

        return {
            "bot_status": self.bot_status,
            "execution_mode": self.execution_mode,
            "strategy_name": self.strategy_name,
            "current_slug": self.current_slug,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "remaining_sec": round(self.remaining_sec, 1),
            "phase": self.phase,
            "is_trading_allowed": self.is_trading_allowed,
            "up_prob": round(self.up_prob, 4) if self.up_prob is not None else None,
            "down_prob": round(self.down_prob, 4) if self.down_prob is not None else None,
            "up_bid": round(self.up_bid, 4) if self.up_bid is not None else None,
            "down_bid": round(self.down_bid, 4) if self.down_bid is not None else None,
            "initial_range_met": self.initial_range_met,
            "reversal_range_met": self.reversal_range_met,
            "initial_order_triggered": self.initial_order_triggered,
            "initial_side": self.initial_side,
            "reversal_order_triggered": self.reversal_order_triggered,
            "feed_status": feed_quality,
            "staleness_sec": staleness,
            "latency_ms": round(self.last_latency_ms, 1),
            "capital": self.capital_state,
            "multi_market": self.multi_market,
            "schedule": sched,
            "server_time": now,
            "server_time_str": ist_datetime_str(now),
            "logs": list(self.logs)[:200],
        }
