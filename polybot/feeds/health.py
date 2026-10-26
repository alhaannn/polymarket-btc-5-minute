"""
Feed health monitoring: staleness, gap detection, and oracle vs spot divergence alerts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("polybot.feeds.health")


@dataclass
class FeedGap:
    feed_name: str
    gap_duration_sec: float
    start_ts_ms: int
    end_ts_ms: int


@dataclass
class FeedState:
    last_price: Optional[float] = None
    last_tick_ts_ms: Optional[int] = None
    last_received_local_ts: Optional[float] = None
    tick_count: int = 0
    gaps: List[FeedGap] = field(default_factory=list)


class FeedHealthTracker:
    """Tracks feed freshness, data gaps, and cross-feed divergence."""

    def __init__(
        self,
        max_staleness_sec: float = 3.0,
        gap_threshold_sec: float = 5.0,
        max_divergence_pct: float = 0.005,  # 0.5%
    ):
        self.max_staleness_sec = max_staleness_sec
        self.gap_threshold_sec = gap_threshold_sec
        self.max_divergence_pct = max_divergence_pct

        self.chainlink = FeedState()
        self.binance = FeedState()

    def record_tick(self, feed: str, price: float, timestamp_ms: int) -> Optional[FeedGap]:
        """Record an incoming tick and detect gaps."""
        now = time.time()
        state = self.chainlink if feed == "chainlink" else self.binance

        detected_gap: Optional[FeedGap] = None
        if state.last_tick_ts_ms is not None:
            delta_sec = (timestamp_ms - state.last_tick_ts_ms) / 1000.0
            if delta_sec >= self.gap_threshold_sec:
                detected_gap = FeedGap(
                    feed_name=feed,
                    gap_duration_sec=delta_sec,
                    start_ts_ms=state.last_tick_ts_ms,
                    end_ts_ms=timestamp_ms,
                )
                state.gaps.append(detected_gap)
                log.warning(
                    "DATA GAP in %s: %.2fs between ticks (%d -> %d)",
                    feed,
                    delta_sec,
                    state.last_tick_ts_ms,
                    timestamp_ms,
                )

        state.last_price = price
        state.last_tick_ts_ms = timestamp_ms
        state.last_received_local_ts = now
        state.tick_count += 1
        return detected_gap

    def get_staleness_sec(self, feed: str) -> float:
        """Returns seconds since the last tick was received locally."""
        state = self.chainlink if feed == "chainlink" else self.binance
        if state.last_received_local_ts is None:
            return float("inf")
        return max(0.0, time.time() - state.last_received_local_ts)

    def is_chainlink_stale(self) -> bool:
        """Rule 3.3: If Chainlink feed is stale (> max_staleness_sec), trading must halt."""
        return self.get_staleness_sec("chainlink") > self.max_staleness_sec

    def get_divergence(self) -> Tuple[Optional[float], bool]:
        """
        Computes |Chainlink - Binance| / Chainlink.
        Returns (divergence_ratio, is_divergent).
        """
        cl_price = self.chainlink.last_price
        bn_price = self.binance.last_price

        if cl_price is None or bn_price is None or cl_price <= 0:
            return None, False

        div = abs(cl_price - bn_price) / cl_price
        is_divergent = div > self.max_divergence_pct
        if is_divergent:
            log.warning(
                "FEED DIVERGENCE ALERT: Chainlink=%.2f, Binance=%.2f (diff=%.3f%% > %.3f%%)",
                cl_price,
                bn_price,
                div * 100.0,
                self.max_divergence_pct * 100.0,
            )
        return div, is_divergent

    def is_healthy(self) -> Tuple[bool, str]:
        """
        Returns (True, 'OK') if safe to trade, or (False, reason) if feed issues exist.
        """
        cl_age = self.get_staleness_sec("chainlink")
        if cl_age > self.max_staleness_sec:
            return False, f"Chainlink stale: {cl_age:.2f}s > {self.max_staleness_sec:.2f}s"

        div, is_div = self.get_divergence()
        if is_div:
            return False, f"Price divergence excessive: {div * 100.0:.2f}%"

        return True, "OK"
