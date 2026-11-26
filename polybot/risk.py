"""
Risk management, circuit breakers, and anomaly guards for polybot.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polybot.config import RiskConfig
from polybot.tz import ist_today_str, session_day_key

log = logging.getLogger("polybot.risk")


class RiskManager:
    """
    Enforces risk limits, circuit breakers, and safety kill switches:
    - Daily / Session loss limit & trailing profit lock circuit breakers
    - Max trades per day limit
    - Consecutive loss cool-down breaker
    - Open exposure limit
    - File-based kill switch ('STOP')
    - Anomaly guards (API error rate, clock drift)
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()

        self.current_day: str = self._current_period_key()
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.session_peak_pnl: float = 0.0
        self.session_shutdown_active: bool = False
        self.session_shutdown_reason: str = ""
        self.consecutive_losses: int = 0
        self.pause_until_window_idx: int = 0
        self.consecutive_api_errors: int = 0
        self.current_open_exposure: float = 0.0

    def _current_period_key(self, current_ts: Optional[float] = None) -> str:
        """Returns session key (13:30 IST boundary) when session_aligned_reset is enabled, else calendar IST date."""
        if getattr(self.config, "session_aligned_reset", False):
            prime_only = getattr(self.config, "prime_hours_only", True)
            return session_day_key(current_ts, prime_hours_only=prime_only)
        return ist_today_str(current_ts)

    def sync_open_exposure(self, exposure_usd: float) -> None:
        """Synchronizes open exposure directly with active positions to prevent drift/leaks."""
        self.current_open_exposure = max(0.0, float(exposure_usd))

    def _check_day_rollover(self, current_ts: Optional[float] = None) -> None:
        today = self._current_period_key(current_ts)
        if today != self.current_day:
            log.info(
                "Session/day rollover (%s -> %s): resetting risk counters (old PnL=$%+.2f, peak=$%+.2f)",
                self.current_day, today, self.daily_pnl, self.session_peak_pnl
            )
            self.current_day = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.session_peak_pnl = 0.0
            self.session_shutdown_active = False
            self.session_shutdown_reason = ""

    def _evaluate_session_shutdown(self) -> None:
        """Checks independent session max-loss, profit-target, and trailing profit-lock triggers."""
        if self.session_shutdown_active:
            return

        max_loss = getattr(self.config, "session_max_loss_usd", None)
        if max_loss is not None and max_loss > 0 and self.daily_pnl <= -abs(max_loss):
            self.session_shutdown_active = True
            self.session_shutdown_reason = f"Session max loss hit: PnL ${self.daily_pnl:.2f} <= -${abs(max_loss):.2f}"
            log.warning("[SESSION_SHUTDOWN] %s", self.session_shutdown_reason)
            return

        profit_tgt = getattr(self.config, "session_profit_target_usd", None)
        if profit_tgt is not None and profit_tgt > 0 and self.daily_pnl >= abs(profit_tgt):
            self.session_shutdown_active = True
            self.session_shutdown_reason = f"Session profit target hit: PnL +${self.daily_pnl:.2f} >= +${abs(profit_tgt):.2f}"
            log.warning("[SESSION_SHUTDOWN] %s", self.session_shutdown_reason)
            return

        trail_arm = getattr(self.config, "trailing_arm_usd", None)
        trail_drop = getattr(self.config, "trailing_drop_usd", None)
        if trail_arm is not None and trail_drop is not None and trail_arm > 0 and trail_drop > 0:
            if self.session_peak_pnl >= trail_arm and (self.session_peak_pnl - self.daily_pnl) >= trail_drop:
                giveback = self.session_peak_pnl - self.daily_pnl
                self.session_shutdown_active = True
                self.session_shutdown_reason = (
                    f"Trailing profit lock triggered: PnL ${self.daily_pnl:+.2f} "
                    f"(Peak +${self.session_peak_pnl:.2f}, drop -${giveback:.2f} >= ${trail_drop:.2f})"
                )
                log.warning("[SESSION_SHUTDOWN] %s", self.session_shutdown_reason)

    def hydrate_session_trades(self, trades: List[Dict[str, Any]], current_ts: Optional[float] = None) -> None:
        """Replays settled trades for the active period from ledger DB so restarts preserve session PnL and locks."""
        self._check_day_rollover(current_ts)
        target_key = self.current_day
        matching = []
        for t in trades or []:
            ts = t.get("exec_ts") or t.get("settled_at_ts")
            if ts is None:
                continue
            if self._current_period_key(float(ts)) != target_key:
                continue
            matching.append(t)

        matching.sort(key=lambda x: (x.get("exec_ts") or 0.0, x.get("settled_at_ts") or 0.0))
        for t in matching:
            self.daily_trades += 1
            if t.get("status") == "SETTLED" and t.get("pnl_usd") is not None:
                pnl = float(t["pnl_usd"])
                self.daily_pnl += pnl
                if self.daily_pnl > self.session_peak_pnl:
                    self.session_peak_pnl = self.daily_pnl
                self._evaluate_session_shutdown()
            elif t.get("status") != "SETTLED":
                self.current_open_exposure += float(t.get("stake_usd") or 0.0)

        if matching:
            log.info(
                "Hydrated session %s from ledger: %d trades, PnL=$%+.2f, Peak=$%+.2f, Shutdown=%s",
                target_key, self.daily_trades, self.daily_pnl, self.session_peak_pnl, self.session_shutdown_active
            )

    def record_trade_placed(self, cost_usd: float, current_ts: Optional[float] = None) -> None:
        self._check_day_rollover(current_ts)
        self.daily_trades += 1
        self.current_open_exposure += cost_usd

    def record_trade_rejected(self, cost_usd: float) -> None:
        self.daily_trades = max(0, self.daily_trades - 1)
        self.current_open_exposure = max(0.0, self.current_open_exposure - cost_usd)

    def record_trade_settled(self, pnl_usd: float, cost_usd: float, current_window_idx: int, current_ts: Optional[float] = None) -> None:
        self._check_day_rollover(current_ts)
        self.daily_pnl += pnl_usd
        if self.daily_pnl > self.session_peak_pnl:
            self.session_peak_pnl = self.daily_pnl
        self.current_open_exposure = max(0.0, self.current_open_exposure - cost_usd)

        if pnl_usd < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.pause_consecutive_losses:
                self.pause_until_window_idx = current_window_idx + self.config.pause_duration_windows
                log.warning(
                    "CIRCUIT BREAKER: %d consecutive losses! Pausing trading for %d windows (until window %d)",
                    self.consecutive_losses, self.config.pause_duration_windows, self.pause_until_window_idx
                )
        else:
            self.consecutive_losses = 0

        self._evaluate_session_shutdown()

    def record_api_error(self) -> None:
        self.consecutive_api_errors += 1
        if self.consecutive_api_errors >= 5:
            log.warning("ANOMALY GUARD: %d consecutive API errors detected", self.consecutive_api_errors)

    def record_api_success(self) -> None:
        self.consecutive_api_errors = 0

    def is_kill_switch_active(self) -> bool:
        """Rule 3.8: Returns True if file named 'STOP' exists in the working directory."""
        return Path(self.config.kill_switch_file).exists()

    def get_effective_daily_loss_limit(self, active_capital_usd: Optional[float] = None) -> float:
        """
        Computes dynamic daily loss limit:
        max(daily_loss_min_usd, active_capital_usd * daily_loss_limit_pct)
        """
        base_limit = abs(self.config.daily_loss_limit_usd)
        if active_capital_usd is not None and active_capital_usd > 0:
            pct = getattr(self.config, "daily_loss_limit_pct", 0.30)
            min_usd = getattr(self.config, "daily_loss_min_usd", 6.0)
            return max(min_usd, round(active_capital_usd * pct, 2))
        return base_limit

    def check_trade_allowed(
        self,
        proposed_stake_usd: float,
        current_window_idx: int,
        active_capital_usd: Optional[float] = None,
        current_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a new trade is permitted under current risk parameters.
        Returns (allowed: bool, reason: str).
        """
        self._check_day_rollover(current_ts)

        # 0. Session shutdown check (Session Max Loss / Profit Target / Trailing Lock)
        if self.session_shutdown_active:
            return False, self.session_shutdown_reason or "Session shutdown active"

        # 1. Kill switch check
        if self.is_kill_switch_active():
            return False, f"Kill switch active: '{self.config.kill_switch_file}' file exists"

        # 2. Consecutive loss pause breaker (e.g. 3 losses in a row -> 15 min cool-down)
        if current_window_idx < self.pause_until_window_idx:
            windows_remaining = self.pause_until_window_idx - current_window_idx
            return False, f"Consecutive loss cooldown active: {windows_remaining} windows remaining (~{windows_remaining * 5}m)"

        # 3. Dynamic Daily loss limit check
        disable_daily = (
            getattr(self.config, "disable_daily_shutdown", False)
            or getattr(self.config, "daily_loss_limit_usd", 25.0) <= 0
        )
        if not disable_daily:
            effective_limit = self.get_effective_daily_loss_limit(active_capital_usd)
            if self.daily_pnl <= -abs(effective_limit):
                return False, f"Daily loss limit reached: PnL ${self.daily_pnl:.2f} <= -${effective_limit:.2f}"

        # 4. Max trades per day check
        if self.daily_trades >= self.config.max_trades_per_day:
            return False, f"Max daily trades reached: {self.daily_trades} >= {self.config.max_trades_per_day}"

        # 5. Open exposure cap
        if self.current_open_exposure + proposed_stake_usd > self.config.max_open_exposure_usd:
            return False, f"Open exposure cap exceeded: ${self.current_open_exposure + proposed_stake_usd:.2f} > ${self.config.max_open_exposure_usd:.2f}"

        # 6. Consecutive API errors anomaly guard
        if self.consecutive_api_errors >= 5:
            return False, f"Too many consecutive API errors: {self.consecutive_api_errors}"

        return True, "OK"
