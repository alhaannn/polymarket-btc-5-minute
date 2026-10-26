"""
Pre-trade filters for polybot.
Guards against adverse drift, volatility spikes, illiquid order books,
feed staleness, and insufficient statistical edge.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.feeds.health import FeedHealthTracker

log = logging.getLogger("polybot.filters")


@dataclass
class FilterResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)

    def add_failure(self, reason: str) -> None:
        self.passed = False
        self.reasons.append(reason)


class FilterEngine:
    """Evaluates safety and liquidity filters prior to order placement."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.lead_history: List[Tuple[float, float]] = []  # [(timestamp_sec, lead_usd)]

    def record_lead(self, ts_sec: float, lead_usd: float) -> None:
        """Keep a rolling 90-second history of leads for adverse drift detection."""
        self.lead_history.append((ts_sec, lead_usd))
        cutoff = ts_sec - 90.0
        while self.lead_history and self.lead_history[0][0] < cutoff:
            self.lead_history.pop(0)

    def check_lead_size(self, z_score: float, lead_usd: float) -> Optional[str]:
        if abs(lead_usd) < self.config.min_lead_usd:
            return f"Lead too small in dollars: ${abs(lead_usd):.2f} < ${self.config.min_lead_usd:.2f}"
        if z_score < self.config.z_min:
            return f"Z-score too low: {z_score:.2f} < {self.config.z_min:.2f}"
        return None

    def check_adverse_drift(self, side: str, window_sec: float = 30.0) -> Optional[str]:
        """
        Check if the favorable lead has eroded by more than adverse_drift_max_pct
        over the last window_sec.
        """
        if len(self.lead_history) < 2:
            return None

        now = self.lead_history[-1][0]
        cur_lead = self.lead_history[-1][1]
        target_ts = now - window_sec

        # Find closest historical point
        past_point = min(self.lead_history, key=lambda x: abs(x[0] - target_ts))
        if now - past_point[0] < window_sec * 0.5:
            return None  # Not enough lookback history yet

        past_lead = past_point[1]
        # For 'Down', a positive lead for the position means price is below K
        # If lead shrunk from e.g. +$50 to +$20, erosion is (50 - 20) / 50 = 60%
        if past_lead > 0 and cur_lead < past_lead:
            erosion = (past_lead - cur_lead) / past_lead
            if erosion > self.config.adverse_drift_max_pct:
                return f"Adverse drift over {window_sec:.0f}s: lead eroded by {erosion * 100.0:.1f}% > {self.config.adverse_drift_max_pct * 100.0:.1f}%"

        return None

    def check_vol_spike(self, vol_spike_ratio: float) -> Optional[str]:
        if vol_spike_ratio > self.config.vol_spike_ratio_max:
            return f"Vol spike: short/long ratio {vol_spike_ratio:.2f} > {self.config.vol_spike_ratio_max:.2f}"
        return None

    def check_order_book(self, book: Optional[TokenOrderBook], stake_usd: float) -> Tuple[Optional[str], Optional[Tuple[float, float, float]]]:
        if book is None:
            return "Order book not initialized", None

        # Check staleness
        book_age = time.time() - book.last_update_ts if book.last_update_ts > 0 else float("inf")
        if book_age > 5.0:
            return f"Order book stale: {book_age:.1f}s > 5.0s", None

        # Check spread
        spread = book.spread()
        if spread is None or spread > self.config.spread_max:
            sp_str = f"{spread:.3f}" if spread is not None else "None"
            return f"Spread too wide or empty: {sp_str} > {self.config.spread_max:.3f}", None

        # Check depth simulation
        fill = book.simulate_buy(stake_usd)
        if fill is None:
            return f"Insufficient ask depth for ${stake_usd:.2f} stake", None

        avg_price, shares, spent = fill
        best_ask = book.best_ask() or avg_price
        slippage = (avg_price - best_ask) / best_ask if best_ask > 0 else 0.0
        if slippage > self.config.max_slippage:
            return f"Slippage too high: {slippage * 100.0:.2f}% > {self.config.max_slippage * 100.0:.2f}%", None

        return None, fill

    def check_feed_health(self, health: Optional[FeedHealthTracker]) -> Optional[str]:
        if health is None:
            return None
        is_healthy, reason = health.is_healthy()
        if not is_healthy:
            return f"Feed unhealthy: {reason}"
        return None

    def evaluate_entry(
        self,
        side: str,
        z_score: float,
        lead_usd: float,
        vol_spike_ratio: float,
        book: Optional[TokenOrderBook],
        stake_usd: float,
        health: Optional[FeedHealthTracker],
    ) -> Tuple[FilterResult, Optional[Tuple[float, float, float]]]:
        """Runs the complete entry filter suite."""
        res = FilterResult(passed=True)

        # 1. Lead size
        f_lead = self.check_lead_size(z_score, lead_usd)
        if f_lead:
            res.add_failure(f_lead)

        # 2. Adverse drift
        f_drift = self.check_adverse_drift(side, window_sec=30.0)
        if f_drift:
            res.add_failure(f_drift)

        # 3. Volatility spike
        f_vol = self.check_vol_spike(vol_spike_ratio)
        if f_vol:
            res.add_failure(f_vol)

        # 4. Feed health
        f_feed = self.check_feed_health(health)
        if f_feed:
            res.add_failure(f_feed)

        # 5. Order book liquidity & depth
        f_book, fill = self.check_order_book(book, stake_usd)
        if f_book:
            res.add_failure(f_book)

        return res, fill
