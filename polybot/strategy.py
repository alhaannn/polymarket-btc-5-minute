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
from polybot.execution.fees import calculate_polymarket_5m_crypto_fee
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
    signal_type: str = "INITIAL"  # "INITIAL" or "REVERSAL"
    linked_order_id: Optional[str] = None
    trigger_prob: float = 0.0
    counterpart_prob: float = 0.0
    data_receipt_ts: float = 0.0


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


class ReversalStrategy(BaseStrategy):
    """
    Polymarket 5-Minute UP/DOWN Strategy (NEW_STRATEGY.md):
    - 0-179 seconds: Waiting phase, observe only, NO trades.
    - 180-299 seconds: Trading phase.
        - Initial entry: When either UP or DOWN outcome probability is 89% to 92% inclusive,
          trigger a $1 paper order for that outcome.
        - Reversal entry: After initial order, continuously monitor opposite outcome.
          When opposite outcome probability is 85% to 90% inclusive,
          trigger a reversal paper order for 10x initial amount ($10).
    - >= 300 seconds: Closed phase, no new entries.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        super().__init__(config or StrategyConfig())
        self.window_start: int = 0
        self.strike_price_K: Optional[float] = None
        self.initial_order_placed: bool = False
        self.initial_side: Optional[str] = None
        self.initial_order_id: Optional[str] = None
        self.initial_order_ts: float = 0.0
        self.initial_shares: float = 0.0
        self.initial_cost_usd: float = 0.0
        self.early_exit_placed: bool = False
        self.early_exit_loss: float = 0.0
        self.early_exit_received: float = 0.0
        self.reversal_order_placed: bool = False
        self.reversal_stage: int = 0
        self.shares_held: Dict[str, float] = {"Up": 0.0, "Down": 0.0}
        self.cost_spent: Dict[str, float] = {"Up": 0.0, "Down": 0.0}
        self.total_spent_usd: float = 0.0
        self.current_side: Optional[str] = None
        self.spot_lead_start_ts: Dict[str, float] = {"Up": 0.0, "Down": 0.0}
        self.last_signal_spent: float = 0.0
        self.last_signal_shares: float = 0.0
        self.last_signal_side: Optional[str] = None
        self.dynamic_initial_usd: Optional[float] = None
        self.dynamic_reversal_usd: Optional[float] = None
        self.rejection_cooldown_until: Dict[str, float] = {}
        self.last_tick_ts: float = 0.0

    def set_dynamic_budgets(self, initial_usd: Optional[float], reversal_usd: Optional[float]) -> None:
        """Sets capital-derived dynamic order budgets for the strategy."""
        self.dynamic_initial_usd = initial_usd
        self.dynamic_reversal_usd = reversal_usd

    def reset_window(self, window_start: int, strike_price_K: Optional[float] = None) -> None:
        self.window_start = window_start
        self.strike_price_K = strike_price_K
        self.initial_order_placed = False
        self.initial_side = None
        self.initial_order_id = None
        self.initial_order_ts = 0.0
        self.initial_shares = 0.0
        self.initial_cost_usd = 0.0
        self.early_exit_placed = False
        self.early_exit_loss = 0.0
        self.early_exit_received = 0.0
        self.reversal_order_placed = False
        self.reversal_stage = 0
        self.shares_held = {"Up": 0.0, "Down": 0.0}
        self.cost_spent = {"Up": 0.0, "Down": 0.0}
        self.total_spent_usd = 0.0
        self.current_side = None
        self.spot_lead_start_ts = {"Up": 0.0, "Down": 0.0}
        self.last_signal_spent = 0.0
        self.last_signal_shares = 0.0
        self.last_signal_side = None
        self.rejection_cooldown_until = {}
        self.last_tick_ts = 0.0

    def on_order_rejected(
        self,
        signal_type: str,
        cooldown_sec: float = 0.0,
        current_ts: Optional[float] = None,
    ) -> None:
        """
        Called when an order signal is rejected (e.g. by RiskManager or Budget Sizing).
        Applies a brief cooldown when requested to prevent tight millisecond log spam loops.
        """
        now_ts = current_ts if current_ts is not None else self.last_tick_ts
        if cooldown_sec > 0 and now_ts > 0:
            self.rejection_cooldown_until[signal_type] = now_ts + cooldown_sec
        else:
            self.rejection_cooldown_until.pop(signal_type, None)

        if signal_type == "INITIAL":
            self.initial_order_placed = False
            self.initial_side = None
            self.initial_order_id = None
            self.initial_shares = 0.0
            self.initial_cost_usd = 0.0
            self.shares_held = {"Up": 0.0, "Down": 0.0}
            self.cost_spent = {"Up": 0.0, "Down": 0.0}
            self.total_spent_usd = 0.0
            self.current_side = None
            self.reversal_stage = 0
            self.last_signal_spent = 0.0
            self.last_signal_shares = 0.0
            self.last_signal_side = None
        elif signal_type in ("REVERSAL", "REVERSAL_1"):
            self.reversal_order_placed = False
            if self.reversal_stage == 1:
                self.reversal_stage = 0
                if self.last_signal_side:
                    self.shares_held[self.last_signal_side] = max(0.0, self.shares_held.get(self.last_signal_side, 0.0) - self.last_signal_shares)
                    self.cost_spent[self.last_signal_side] = max(0.0, self.cost_spent.get(self.last_signal_side, 0.0) - self.last_signal_spent)
                    self.total_spent_usd = max(0.0, self.total_spent_usd - self.last_signal_spent)
                    self.last_signal_spent = 0.0
                    self.last_signal_shares = 0.0
                    self.last_signal_side = None
        elif signal_type == "REVERSAL_2":
            if self.reversal_stage == 2:
                self.reversal_stage = 1
                if self.last_signal_side:
                    self.shares_held[self.last_signal_side] = max(0.0, self.shares_held.get(self.last_signal_side, 0.0) - self.last_signal_shares)
                    self.cost_spent[self.last_signal_side] = max(0.0, self.cost_spent.get(self.last_signal_side, 0.0) - self.last_signal_spent)
                    self.total_spent_usd = max(0.0, self.total_spent_usd - self.last_signal_spent)
                    self.last_signal_spent = 0.0
                    self.last_signal_shares = 0.0
                    self.last_signal_side = None
        elif signal_type == "EARLY_EXIT":
            self.early_exit_placed = False
            self.early_exit_loss = 0.0
            self.early_exit_received = 0.0

    def check_spot_lead_confirmed(
        self,
        side: str,
        current_spot: float,
        timestamp_sec: float,
    ) -> bool:
        """
        Validates whether Binance spot price leads the strike price in the direction of 'side'
        by at least reversal_spot_lead_usd, held continuously for reversal_persistence_sec.
        If strike_price_K is None or 0, returns True (e.g. tests without strike).
        """
        if self.strike_price_K is None or self.strike_price_K <= 0:
            return True

        min_lead = getattr(self.config, "reversal_spot_lead_usd", 15.0)
        persistence = getattr(self.config, "reversal_persistence_sec", 1.5)

        lead = (current_spot - self.strike_price_K) if side == "Up" else (self.strike_price_K - current_spot)
        if lead >= min_lead:
            first_ts = self.spot_lead_start_ts.get(side, 0.0)
            if first_ts <= 0.0:
                self.spot_lead_start_ts[side] = timestamp_sec
                return persistence <= 0.0
            return (timestamp_sec - first_ts) >= persistence
        else:
            self.spot_lead_start_ts[side] = 0.0
            return False

    def calculate_exact_recovery_stake(
        self,
        target_side: str,
        entry_price: float,
    ) -> float:
        """
        Calculates exact base stake to achieve net $0.00 recovery across window,
        crediting any shares already held on target_side.
        Formula:
          deficit = max(0, total_spent_usd - shares_held[target_side] * 1.0)
          base_stake = deficit / ( (1 / (entry_price * (1 + 0.07*(1 - entry_price)))) - 1 )
        """
        held_shares = self.shares_held.get(target_side, 0.0)
        expected_payout_held = held_shares * 1.0
        deficit = max(0.0, self.total_spent_usd - expected_payout_held)
        if deficit <= 0.01:
            return 1.0

        fee_rate = 0.07 * (1.0 - entry_price)
        effective_cost_per_share = entry_price * (1.0 + fee_rate)
        if effective_cost_per_share >= 1.0:
            return 1.0
        denom = (1.0 / effective_cost_per_share) - 1.0
        if denom <= 0.02:
            return 1.0

        base_stake = deficit / denom
        return max(1.0, round(base_stake, 2))

    def on_tick(
        self,
        current_price: float,
        timestamp_sec: float,
        books: Dict[str, TokenOrderBook],
        health: Optional[FeedHealthTracker] = None,
        data_receipt_ts: Optional[float] = None,
    ) -> Optional[TradeSignal]:
        self.last_tick_ts = timestamp_sec
        elapsed = timestamp_sec - self.window_start
        receipt_ts = data_receipt_ts or timestamp_sec

        # 0. Prime Hours enforcement (Option B: 13:30 to 01:00 IST)
        # Apply only to real-world timestamps (> 1_000_000_000, post-2001)
        if getattr(self.config, "prime_hours_only", False) and timestamp_sec > 1_000_000_000:
            import datetime
            dt_ist = datetime.datetime.fromtimestamp(timestamp_sec, tz=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
            h, m = dt_ist.hour, dt_ist.minute
            is_prime = (h > 13) or (h == 13 and m >= 30) or (h == 0) or (h == 1 and m == 0 and dt_ist.second == 0)
            if not is_prime:
                return None

        # Rejection cooldown check to eliminate millisecond retry spam loops
        if not self.initial_order_placed and self.rejection_cooldown_until.get("INITIAL", 0.0) > timestamp_sec:
            return None
        if self.initial_order_placed and not self.reversal_order_placed and self.rejection_cooldown_until.get("REVERSAL", 0.0) > timestamp_sec:
            return None

        # 1. Strict time-window enforcement
        # Waiting phase: first 180 seconds -> NO trades
        if elapsed < self.config.reversal_window_start_sec:
            return None

        # Closed phase for initial snipe: initial order MUST enter within snipe window (default <= 210s)
        snipe_cutoff = getattr(self.config, "initial_snipe_end_sec", getattr(self.config, "reversal_window_end_sec", 210.0))
        if not self.initial_order_placed and elapsed >= snipe_cutoff:
            return None

        # Closed phase: at or after 300 seconds -> window concluded, NO trades
        if elapsed >= 300.0:
            return None

        # 2. Extract books for Up and Down (supporting 'Up'/'Down' keys or side property)
        book_up = books.get("Up")
        book_down = books.get("Down")
        if book_up is None or book_down is None:
            for k, b in books.items():
                if k.lower() == "up" or getattr(b, "side", "").lower() == "up":
                    book_up = b
                elif k.lower() == "down" or getattr(b, "side", "").lower() == "down":
                    book_down = b

        # Extract best asks with prediction market complement derivation: ask(Up) = 1.0 - bid(Down)
        up_prob = book_up.best_ask() if book_up else None
        down_prob = book_down.best_ask() if book_down else None

        if up_prob is None and book_down and book_down.best_bid() is not None:
            up_prob = round(1.0 - book_down.best_bid(), 4)
        if down_prob is None and book_up and book_up.best_bid() is not None:
            down_prob = round(1.0 - book_up.best_bid(), 4)

        # If data is completely missing, do not trigger
        if up_prob is None and down_prob is None:
            return None

        # 3. Check INITIAL Entry (if not yet placed)
        if not self.initial_order_placed:
            for side, prob, counterpart_prob, book, opp_b in (
                ("Up", up_prob, down_prob, book_up, book_down),
                ("Down", down_prob, up_prob, book_down, book_up),
            ):
                if prob is None or book is None:
                    continue

                # Range check: 89% <= prob <= 90% (or configured range)
                if self.config.initial_prob_min <= prob <= self.config.initial_prob_max:
                    max_cp = getattr(self.config, "max_initial_counterpart_prob", None)
                    if max_cp is not None and counterpart_prob is not None and counterpart_prob > max_cp + 1e-5:
                        log.info(
                            "ReversalStrategy %s ask %.3f skipped: counterpart_prob %.3f > max %.3f (wide spread)",
                            side, prob, counterpart_prob, max_cp
                        )
                        continue
                    initial_stake = self.dynamic_initial_usd if self.dynamic_initial_usd is not None else self.config.initial_stake_usd
                    opp_bids = opp_b.get_sorted_bids() if opp_b else None
                    fill = book.simulate_buy(initial_stake, opposing_bids=opp_bids)
                    if fill is None:
                        log.info(
                            "ReversalStrategy %s ask %.3f in initial range [%.2f, %.2f] but depth thin for $%.2f",
                            side, prob, self.config.initial_prob_min, self.config.initial_prob_max,
                            initial_stake
                        )
                        continue

                    avg_price, shares, spent = fill
                    # Hard Price Ceiling check (Option B safeguard: never buy above price_ceiling)
                    price_ceil = getattr(self.config, "price_ceiling", 0.910)
                    if avg_price > price_ceil + 1e-5:
                        log.info(
                            "ReversalStrategy %s avg_price %.3f exceeds price ceiling %.3f (stake=$%.2f)",
                            side, avg_price, price_ceil, initial_stake
                        )
                        continue

                    fee = calculate_polymarket_5m_crypto_fee(shares, avg_price)
                    order_id = f"init-{int(timestamp_sec * 1000)}"

                    self.initial_order_placed = True
                    self.initial_side = side
                    self.initial_order_id = order_id
                    self.initial_order_ts = timestamp_sec
                    self.initial_shares = shares
                    self.initial_cost_usd = spent
                    self.shares_held[side] = shares
                    self.cost_spent[side] = spent
                    self.total_spent_usd = spent
                    self.current_side = side
                    self.reversal_stage = 0

                    log.info(
                        "REVERSAL_STRAT INITIAL BUY %s @ %.3f x %.2f shares ($%.2f) at elapsed=%.1fs (prob=%.3f)",
                        side, avg_price, shares, spent, elapsed, prob
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
                        model_prob=prob,
                        expected_edge=0.0,
                        timestamp_sec=timestamp_sec,
                        signal_type="INITIAL",
                        trigger_prob=prob,
                        counterpart_prob=counterpart_prob or 0.0,
                        data_receipt_ts=receipt_ts,
                    )

        # 4. Check EARLY EXIT or REVERSAL Entry
        elif self.initial_order_placed and self.initial_side is not None:
            opp_side = "Down" if self.initial_side == "Up" else "Up"
            opp_book = book_down if self.initial_side == "Up" else book_up
            opp_prob = down_prob if self.initial_side == "Up" else up_prob
            initial_book = book_up if self.initial_side == "Up" else book_down
            init_current_prob = up_prob if self.initial_side == "Up" else down_prob

            early_exit_enabled = getattr(self.config, "early_exit", True)
            enable_rev = getattr(self.config, "enable_reversals", True)

            # Phase A: Check EARLY EXIT (Shield 2: Order Book Capital Salvage)
            if early_exit_enabled and not getattr(self, "early_exit_placed", False):
                if self.rejection_cooldown_until.get("EARLY_EXIT", 0.0) <= timestamp_sec:
                    exit_p = getattr(self.config, "exit_p", 0.35)
                    init_bid = initial_book.best_bid() if initial_book else None
                    should_early_exit = False
                    if init_bid is not None and init_bid <= exit_p:
                        should_early_exit = True
                    elif opp_prob is not None and opp_prob >= self.config.reversal_prob_min:
                        should_early_exit = True

                    if should_early_exit and initial_book and getattr(self, "initial_shares", 0.0) > 0:
                        sell_fill = initial_book.simulate_sell(self.initial_shares)
                        if sell_fill is not None:
                            avg_sell_price, shares_sold, received = sell_fill
                            fee = received * self.config.taker_fee_rate
                            self.early_exit_placed = True
                            self.early_exit_received = received
                            self.early_exit_loss = max(0.0, self.initial_cost_usd - received + fee)
                            log.info(
                                "REVERSAL_STRAT EARLY EXIT SELL %s @ %.3f x %.2f shares ($%.2f received, loss=$%.2f) at elapsed=%.1fs (init_bid=%.3f, opp_prob=%.3f)",
                                self.initial_side, avg_sell_price, shares_sold, received,
                                self.early_exit_loss, elapsed, init_bid or 0.0, opp_prob or 0.0
                            )
                            return TradeSignal(
                                slug=f"btc-updown-5m-{self.window_start}",
                                side=self.initial_side,
                                action="SELL",
                                order_type="taker",
                                price=avg_sell_price,
                                shares=shares_sold,
                                cost_usd=received,
                                fee_usd=fee,
                                elapsed_sec=elapsed,
                                model_prob=init_bid or 0.0,
                                expected_edge=0.0,
                                timestamp_sec=timestamp_sec,
                                signal_type="EARLY_EXIT",
                                linked_order_id=self.initial_order_id,
                                trigger_prob=init_bid or 0.0,
                                counterpart_prob=opp_prob or 0.0,
                                data_receipt_ts=receipt_ts,
                            )

            # Phase B: Check REVERSAL Entry (Shield 3: Multi-Stage Precision Recovery Ladder & Shield 4 Guardrails)
            max_stages = getattr(self.config, "max_reversal_stages", 2)
            if enable_rev and self.reversal_stage < max_stages:
                # If early exit is enabled, reversal only triggers AFTER early exit has executed!
                if early_exit_enabled and not getattr(self, "early_exit_placed", False):
                    return None

                flip_mode = getattr(self.config, "recovery_flip_mode", "fixed")

                # Determine target leg and direction based on current reversal stage
                if self.reversal_stage == 0:
                    target_side = opp_side
                    target_book = opp_book
                    target_prob = opp_prob
                    counterpart_p = init_current_prob
                    p_min = self.config.reversal_prob_min
                    p_max = self.config.reversal_prob_max
                    max_cutoff = getattr(self.config, "recovery_max_time_sec", 275.0)
                    sig_type = "REVERSAL_1" if flip_mode == "precision" else "REVERSAL"
                elif self.reversal_stage == 1:
                    target_side = self.initial_side
                    target_book = initial_book
                    target_prob = init_current_prob
                    counterpart_p = opp_prob
                    p_min = getattr(self.config, "reversal_stage2_prob_min", self.config.reversal_prob_min)
                    p_max = getattr(self.config, "reversal_stage2_prob_max", self.config.reversal_prob_max)
                    max_cutoff = getattr(self.config, "recovery_stage2_max_time_sec", 280.0)
                    sig_type = "REVERSAL_2"
                else:
                    return None

                if self.rejection_cooldown_until.get(sig_type, 0.0) > timestamp_sec or self.rejection_cooldown_until.get("REVERSAL", 0.0) > timestamp_sec:
                    return None

                # Hard Timing Cutoff
                if elapsed > max_cutoff:
                    log.debug("Reversal rejected: elapsed %.1fs exceeds cutoff %.1fs", elapsed, max_cutoff)
                    return None

                # Reversal debounce
                debounce_sec = getattr(self.config, "reversal_debounce_sec", 0.0)
                if debounce_sec > 0 and (timestamp_sec - self.initial_order_ts) < debounce_sec:
                    return None

                # Collapse check: counterpart side must have collapsed <= 0.35 (or early exit confirmed)
                if not getattr(self, "early_exit_placed", False):
                    if counterpart_p is not None and counterpart_p > 0.35:
                        return None

                # Spread safety guard: only block if synthetic spread is severely distorted (> 1.15)
                if target_prob is not None and counterpart_p is not None:
                    if (target_prob + counterpart_p) > 1.15:
                        return None

                if target_prob is not None and target_book is not None:
                    if p_min <= target_prob <= p_max:
                        # Binance Spot Lead & Persistence confirmation
                        if not self.check_spot_lead_confirmed(target_side, current_price, timestamp_sec):
                            return None

                        budget_limit = self.dynamic_reversal_usd if self.dynamic_reversal_usd is not None else self.config.reversal_stake_usd

                        if flip_mode == "precision":
                            reversal_stake = self.calculate_exact_recovery_stake(target_side, target_prob)
                            reversal_stake = min(reversal_stake, budget_limit)
                            # Enforce cumulative window risk cap for precision ladder
                            ladder_cap = getattr(self.config, "ladder_max_window_risk_usd", 10.0)
                            remaining_window_risk = max(0.0, ladder_cap - self.total_spent_usd)
                            if reversal_stake > remaining_window_risk:
                                if remaining_window_risk >= 1.0:
                                    reversal_stake = round(remaining_window_risk, 2)
                                else:
                                    log.info(
                                        "Reversal rejected: window risk cap reached ($%.2f / $%.2f)",
                                        self.total_spent_usd, ladder_cap
                                    )
                                    return None
                        else:
                            # Exact 10x Sizing ($1 initial -> $10 reversal, $2 initial -> $20 reversal)
                            reversal_mult = getattr(self.config, "reversal_multiplier", 10.0)
                            target_stake = round(self.initial_cost_usd * reversal_mult, 2)
                            reversal_stake = min(target_stake, budget_limit)
                            reversal_stake = max(1.0, reversal_stake)

                        opposing = initial_book if target_side == opp_side else opp_book
                        opp_bids = opposing.get_sorted_bids() if opposing else None
                        fill = target_book.simulate_buy(reversal_stake, opposing_bids=opp_bids)
                        if fill is None:
                            log.info(
                                "ReversalStrategy %s ask %.3f in reversal range [%.2f, %.2f] but depth thin for $%.2f",
                                target_side, target_prob, p_min, p_max, reversal_stake
                            )
                            return None

                        avg_price, shares, spent = fill
                        # Price ceiling check on reversal
                        price_ceil = getattr(self.config, "reversal_price_ceiling", 0.930)
                        if avg_price > price_ceil + 1e-5:
                            log.info("ReversalStrategy %s avg_price %.3f exceeds price ceiling %.3f", target_side, avg_price, price_ceil)
                            return None

                        fee = calculate_polymarket_5m_crypto_fee(shares, avg_price)
                        self.reversal_stage += 1
                        self.reversal_order_placed = True
                        self.shares_held[target_side] = self.shares_held.get(target_side, 0.0) + shares
                        self.cost_spent[target_side] = self.cost_spent.get(target_side, 0.0) + spent
                        self.total_spent_usd += spent
                        self.current_side = target_side
                        self.last_signal_spent = spent
                        self.last_signal_shares = shares
                        self.last_signal_side = target_side

                        log.info(
                            "REVERSAL_STRAT %s BUY %s @ %.3f x %.2f shares ($%.2f) at elapsed=%.1fs (prob=%.3f, total_spent=$%.2f, linked=%s)",
                            sig_type, target_side, avg_price, shares, spent, elapsed, target_prob, self.total_spent_usd, self.initial_order_id
                        )
                        return TradeSignal(
                            slug=f"btc-updown-5m-{self.window_start}",
                            side=target_side,
                            action="BUY",
                            order_type="taker",
                            price=avg_price,
                            shares=shares,
                            cost_usd=spent,
                            fee_usd=fee,
                            elapsed_sec=elapsed,
                            model_prob=target_prob,
                            expected_edge=0.0,
                            timestamp_sec=timestamp_sec,
                            signal_type=sig_type,
                            linked_order_id=self.initial_order_id,
                            trigger_prob=target_prob,
                            counterpart_prob=counterpart_p or 0.0,
                            data_receipt_ts=receipt_ts,
                        )

        return None

