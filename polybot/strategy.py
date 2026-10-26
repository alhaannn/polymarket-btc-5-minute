"""
Trading strategy engines for polybot.
Supports both LegacyStrategy (reproducing original bot.py) and
EdgeStrategy (continuous probabilistic evaluation, filters, dynamic edge schedule, and early exit).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.feeds.health import FeedHealthTracker
from polybot.filters import FilterEngine, FilterResult
from polybot.model import ModelOutput, ProbabilityModel, RollingEWMAVol

log = logging.getLogger("polybot.strategy")


@dataclass
class TradeSignal:
    slug: str
    side: str  # "Up" or "Down"
    action: str  # "BUY" or "SELL"
    order_type: str  # "taker" or "maker"
    price: float
    shares: float
    cost_usd: float
    fee_usd: float
    elapsed_sec: float
    model_prob: float
    expected_edge: float
    timestamp_sec: float


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(self, config: StrategyConfig):
        self.config = config

    @abstractmethod
    def reset_window(self, window_start: int, strike_price_K: Optional[float]) -> None:
        pass

    @abstractmethod
    def on_tick(
        self,
        current_price: float,
        timestamp_sec: float,
        books: Dict[str, TokenOrderBook],
        health: Optional[FeedHealthTracker] = None,
    ) -> Optional[TradeSignal]:
        pass


class LegacyStrategy(BaseStrategy):
    """
    Legacy 0.93 - 0.96 late-entry strategy from original bot.py.
    Used for A/B backtesting against EdgeStrategy.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        super().__init__(config or StrategyConfig())
        self.window_start: int = 0
        self.strike_price_K: Optional[float] = None
        self.has_traded: bool = False

    def reset_window(self, window_start: int, strike_price_K: Optional[float]) -> None:
        self.window_start = window_start
        self.strike_price_K = strike_price_K
        self.has_traded = False

    def on_tick(
        self,
        current_price: float,
        timestamp_sec: float,
        books: Dict[str, TokenOrderBook],
        health: Optional[FeedHealthTracker] = None,
    ) -> Optional[TradeSignal]:
        if self.has_traded:
            return None

        elapsed = timestamp_sec - self.window_start
        # Wait until entry window opens
        if not (self.config.entry_start_sec <= elapsed <= self.config.entry_end_sec):
            return None

        for side in ("Up", "Down"):
            book = books.get(side)
            if not book:
                continue

            best_ask = book.best_ask()
            if best_ask is None:
                continue

            # Check legacy band: [0.93, 0.96]
            if not (self.config.legacy_price_min <= best_ask <= self.config.legacy_price_max):
                continue

            fill = book.simulate_buy(self.config.stake_usd)
            if fill is None:
                continue

            avg_price, shares, spent = fill
            if avg_price > self.config.legacy_price_max:
                continue

            fee = spent * self.config.taker_fee_rate
            self.has_traded = True
            log.info(
                "LEGACY BUY %s @ %.3f x %.2f shares ($%.2f) at elapsed=%.1fs",
                side, avg_price, shares, spent, elapsed
            )
            return TradeSignal(
                slug=f"btc-updown-5m-{self.window_start}",
                side=side,
                action="BUY",
                order_type="taker",
                price=avg_price,
                shares=shares,
                cost_usd=spent,
                fee_usd=fee,
                elapsed_sec=elapsed,
                model_prob=best_ask,
                expected_edge=0.0,
                timestamp_sec=timestamp_sec,
            )

        return None


class EdgeStrategy(BaseStrategy):
    """
    Probabilistic Edge Strategy.
    Continuously evaluates Brownian probability, EWMA volatility, pre-trade filters,
    and expected edge: P_calibrated - Ask - Fee >= MIN_EDGE.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        super().__init__(config or StrategyConfig())
        self.model = ProbabilityModel()
        self.ewma_vol = RollingEWMAVol(short_span=30, long_span=180)
        self.filter_engine = FilterEngine(self.config)

        self.window_start: int = 0
        self.strike_price_K: Optional[float] = None
        self.has_traded: bool = False
        self.active_position: Optional[TradeSignal] = None

    def reset_window(self, window_start: int, strike_price_K: Optional[float]) -> None:
        self.window_start = window_start
        self.strike_price_K = strike_price_K
        self.has_traded = False
        self.active_position = None
        self.filter_engine = FilterEngine(self.config)

    def get_dynamic_min_edge(self, time_left_sec: float) -> float:
        """
        Optional dynamic edge schedule:
        As time runs out and uncertainty drops, edge requirement can adjust smoothly.
        """
        base_edge = self.config.min_edge
        # Default: constant min_edge
        return base_edge

    def on_tick(
        self,
        current_price: float,
        timestamp_sec: float,
        books: Dict[str, TokenOrderBook],
        health: Optional[FeedHealthTracker] = None,
    ) -> Optional[TradeSignal]:
        if self.strike_price_K is None or current_price <= 0:
            return None

        # Update rolling volatility with current spot tick
        self.ewma_vol.update(current_price, timestamp_sec)

        elapsed = timestamp_sec - self.window_start
        time_left = max(0.0, 300.0 - elapsed)

        # 1. Check for Early Exit if currently holding an open trade
        if self.has_traded and self.active_position and self.config.early_exit:
            exit_sig = self._check_early_exit(current_price, time_left, books, timestamp_sec)
            if exit_sig:
                return exit_sig

        # If already traded in this window, don't enter again
        if self.has_traded:
            return None

        # 2. Check Entry Window [ENTRY_START, ENTRY_END] (e.g. 120s - 285s)
        if not (self.config.entry_start_sec <= elapsed <= self.config.entry_end_sec):
            return None

        vol_per_sec = self.ewma_vol.get_short_vol_per_sec()
        vol_spike_ratio = self.ewma_vol.get_vol_spike_ratio()
        required_edge = self.get_dynamic_min_edge(time_left)

        for side in ("Up", "Down"):
            book = books.get(side)
            if not book:
                continue

            # Compute calibrated probability
            m_output = self.model.compute_probability(
                current_price=current_price,
                strike_price_K=self.strike_price_K,
                time_left_sec=time_left,
                vol_per_sec=vol_per_sec,
                side=side,
            )

            # Record lead for adverse drift tracking
            self.filter_engine.record_lead(timestamp_sec, m_output.lead_usd)

            # Evaluate pre-trade filter suite
            filter_res, fill = self.filter_engine.evaluate_entry(
                side=side,
                z_score=m_output.z_score,
                lead_usd=m_output.lead_usd,
                vol_spike_ratio=vol_spike_ratio,
                book=book,
                stake_usd=self.config.stake_usd,
                health=health,
            )

            if not filter_res.passed or fill is None:
                continue

            avg_price, shares, spent = fill

            # Check price band: PRICE_MIN <= avg_price <= PRICE_MAX (default [0.85, 0.985])
            if not (self.config.price_min <= avg_price <= self.config.price_max):
                continue

            # Calculate fees & expected edge
            fee_per_share = avg_price * self.config.taker_fee_rate
            edge = m_output.p_calibrated - avg_price - fee_per_share

            # Primary gate: edge >= MIN_EDGE
            if edge < required_edge:
                continue

            total_fee = spent * self.config.taker_fee_rate
            sig = TradeSignal(
                slug=f"btc-updown-5m-{self.window_start}",
                side=side,
                action="BUY",
                order_type=self.config.order_style,
                price=avg_price,
                shares=shares,
                cost_usd=spent,
                fee_usd=total_fee,
                elapsed_sec=elapsed,
                model_prob=m_output.p_calibrated,
                expected_edge=edge,
                timestamp_sec=timestamp_sec,
            )

            self.has_traded = True
            self.active_position = sig
            log.info(
                "EDGE BUY %s @ %.3f x %.2f shares ($%.2f) | P_cal=%.3f Edge=%.3f (t+%.1fs)",
                side, avg_price, shares, spent, m_output.p_calibrated, edge, elapsed
            )
            return sig

        return None

    def _check_early_exit(
        self,
        current_price: float,
        time_left_sec: float,
        books: Dict[str, TokenOrderBook],
        timestamp_sec: float,
    ) -> Optional[TradeSignal]:
        """Section 3.6: Sell position to bids if model probability drops below EXIT_P."""
        pos = self.active_position
        if not pos or pos.shares <= 0 or self.strike_price_K is None:
            return None

        vol_per_sec = self.ewma_vol.get_short_vol_per_sec()
        m_output = self.model.compute_probability(
            current_price=current_price,
            strike_price_K=self.strike_price_K,
            time_left_sec=time_left_sec,
            vol_per_sec=vol_per_sec,
            side=pos.side,
        )

        # Trigger exit if model probability collapses below EXIT_P or lead flips sign
        if m_output.p_calibrated < self.config.exit_p or m_output.lead_usd < 0:
            book = books.get(pos.side)
            if not book:
                return None

            sell_fill = book.simulate_sell(pos.shares)
            if sell_fill is None:
                return None

            avg_exit_price, shares_sold, usd_received = sell_fill
            fee = usd_received * self.config.taker_fee_rate
            log.info(
                "EARLY EXIT %s: selling %.2f shares @ %.3f (rec=$%.2f) because P=%.3f < %.2f",
                pos.side, shares_sold, avg_exit_price, usd_received, m_output.p_calibrated, self.config.exit_p
            )
            # Clear position
            self.active_position = None
            return TradeSignal(
                slug=pos.slug,
                side=pos.side,
                action="SELL",
                order_type="taker",
                price=avg_exit_price,
                shares=shares_sold,
                cost_usd=usd_received,
                fee_usd=fee,
                elapsed_sec=300.0 - time_left_sec,
                model_prob=m_output.p_calibrated,
                expected_edge=0.0,
                timestamp_sec=timestamp_sec,
            )

        return None
