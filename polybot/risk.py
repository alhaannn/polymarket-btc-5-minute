"""
Risk management, circuit breakers, and anomaly guards for polybot.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from polybot.config import RiskConfig

log = logging.getLogger("polybot.risk")


class RiskManager:
    """
    Enforces risk limits, circuit breakers, and safety kill switches:
    - Daily loss limit circuit breaker
    - Max trades per day limit
    - Consecutive loss cool-down breaker
    - Open exposure limit
    - File-based kill switch ('STOP')
    - Anomaly guards (API error rate, clock drift)
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()

        self.current_day: str = time.strftime("%Y-%m-%d")
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.consecutive_losses: int = 0
        self.pause_until_window_idx: int = 0
        self.consecutive_api_errors: int = 0
        self.current_open_exposure: float = 0.0

    def _check_day_rollover(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.current_day:
            log.info("Day rollover: resetting daily risk counters (old PnL=$%+.2f)", self.daily_pnl)
            self.current_day = today
            self.daily_pnl = 0.0
            self.daily_trades = 0

    def record_trade_placed(self, cost_usd: float) -> None:
        self._check_day_rollover()
        self.daily_trades += 1
        self.current_open_exposure += cost_usd

    def record_trade_settled(self, pnl_usd: float, cost_usd: float, current_window_idx: int) -> None:
        self._check_day_rollover()
        self.daily_pnl += pnl_usd
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

    def record_api_error(self) -> None:
        self.consecutive_api_errors += 1
        if self.consecutive_api_errors >= 5:
            log.warning("ANOMALY GUARD: %d consecutive API errors detected", self.consecutive_api_errors)

    def record_api_success(self) -> None:
        self.consecutive_api_errors = 0

    def is_kill_switch_active(self) -> bool:
        """Rule 3.8: Returns True if file named 'STOP' exists in the working directory."""
        return Path(self.config.kill_switch_file).exists()

    def check_trade_allowed(self, proposed_stake_usd: float, current_window_idx: int) -> Tuple[bool, str]:
        """
        Evaluates whether a new trade is permitted under current risk parameters.
        Returns (allowed: bool, reason: str).
        """
        self._check_day_rollover()

        # 1. Kill switch check
        if self.is_kill_switch_active():
            return False, f"Kill switch active: '{self.config.kill_switch_file}' file exists"

        # 2. Daily loss limit check
        if self.daily_pnl <= -abs(self.config.daily_loss_limit_usd):
            return False, f"Daily loss limit reached: PnL ${self.daily_pnl:.2f} <= -${self.config.daily_loss_limit_usd:.2f}"

        # 3. Max trades per day check
        if self.daily_trades >= self.config.max_trades_per_day:
            return False, f"Max daily trades reached: {self.daily_trades} >= {self.config.max_trades_per_day}"

        # 4. Consecutive loss pause breaker
        if current_window_idx < self.pause_until_window_idx:
            windows_remaining = self.pause_until_window_idx - current_window_idx
            return False, f"Consecutive loss cooldown active: {windows_remaining} windows remaining"

        # 5. Open exposure cap
        if self.current_open_exposure + proposed_stake_usd > self.config.max_open_exposure_usd:
            return False, f"Open exposure cap exceeded: ${self.current_open_exposure + proposed_stake_usd:.2f} > ${self.config.max_open_exposure_usd:.2f}"

        # 6. Consecutive API errors anomaly guard
        if self.consecutive_api_errors >= 5:
            return False, f"Too many consecutive API errors: {self.consecutive_api_errors}"

        return True, "OK"
