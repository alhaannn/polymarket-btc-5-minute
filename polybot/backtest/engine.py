"""
Historical event-driven backtesting engine.
Replays recorded ticks through the shared strategy and paper execution modules.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from polybot.backtest.loader import WindowData
from polybot.config import PolybotConfig, StrategyConfig
from polybot.execution.base import Order
from polybot.execution.paper import PaperExecutionEngine
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.feeds.health import FeedHealthTracker
from polybot.strategy import BaseStrategy, EdgeStrategy, LegacyStrategy

log = logging.getLogger("polybot.backtest.engine")


@dataclass
class BacktestTrade:
    slug: str
    side: str
    action: str
    order_type: str
    entry_price: float
    shares: float
    cost_usd: float
    fee_usd: float
    elapsed_sec: float
    model_prob: float
    expected_edge: float
    z_score: float
    vol_regime: str  # "low", "medium", "high"
    hour_of_day: int  # 0 - 23 UTC
    winner: str
    is_win: bool
    payout_usd: float
    pnl_usd: float


class BacktestEngine:
    """Replays historical windows using the production strategy and execution engine."""

    def __init__(self, config: Optional[PolybotConfig] = None):
        self.config = config or PolybotConfig()
        self.execution = PaperExecutionEngine(self.config.strategy)

    def run(
        self,
        windows: List[WindowData],
        strategy: Optional[BaseStrategy] = None,
    ) -> List[BacktestTrade]:
        """Runs the backtest across all provided historical windows."""
        strat = strategy or (
            LegacyStrategy(self.config.strategy)
            if self.config.strategy.strategy_name == "legacy"
            else EdgeStrategy(self.config.strategy)
        )

        completed_trades: List[BacktestTrade] = []

        for win in windows:
            if not win.winner:
                continue  # Skip windows without settlement resolution

            trades = self._run_window(win, strat)
            completed_trades.extend(trades)

        return completed_trades

    def _run_window(self, win: WindowData, strat: BaseStrategy) -> List[BacktestTrade]:
        window_trades: List[BacktestTrade] = []

        # Reset strategy and execution engine for this window
        strat.reset_window(win.window_start, win.strike_price_K)
        health = FeedHealthTracker(
            max_staleness_sec=self.config.feeds.max_feed_staleness_sec,
            max_divergence_pct=self.config.feeds.max_divergence_pct,
        )

        books: Dict[str, TokenOrderBook] = {
            "Up": TokenOrderBook(win.tokens.get("Up", "up_token")),
            "Down": TokenOrderBook(win.tokens.get("Down", "down_token")),
        }
        token_to_side = {
            win.tokens.get("Up", "up_token"): "Up",
            win.tokens.get("Down", "down_token"): "Down",
        }

        current_btc_price = win.strike_price_K
        last_signal = None

        # Determine hour of day (UTC)
        gm_time = time.gmtime(win.window_start)
        hour_of_day = gm_time.tm_hour

        for event in win.events:
            ts = event.timestamp_sec

            if event.event_type == "chainlink":
                price = float(event.payload.get("price", 0.0))
                if price > 0:
                    current_btc_price = price
                    health.record_tick("chainlink", price, int(ts * 1000))

            elif event.event_type == "binance":
                price = float(event.payload.get("price", 0.0))
                if price > 0:
                    health.record_tick("binance", price, int(ts * 1000))

            elif event.event_type == "market":
                self._apply_market_event(event.payload.get("raw", {}), books, token_to_side)

            # Process pending simulated orders (fills after latency_ms)
            fills = self.execution.process_pending_orders(ts, books)

            # Evaluate strategy on every tick
            if current_btc_price > 0 and not strat.has_traded:
                sig = strat.on_tick(current_btc_price, ts, books, health)
                if sig:
                    last_signal = sig
                    order = Order(
                        order_id=f"bt-{int(ts * 1000)}",
                        slug=win.slug,
                        side=sig.side,
                        action=sig.action,
                        order_type=sig.order_type,
                        price=sig.price,
                        usd_amount=self.config.strategy.stake_usd,
                        shares=sig.shares,
                        created_at_ts=ts,
                        latency_ms=self.config.strategy.latency_ms,
                    )
                    self.execution.submit_order(order)

        # Process any remaining orders up to window end
        self.execution.process_pending_orders(win.window_end, books)

        # Settle positions for this window
        settled_pos = self.execution.settle_window(win.slug, winner=win.winner, settle_ts=win.window_end)
        if settled_pos and last_signal:
            # Classify volatility regime
            vol_per_sec = getattr(strat, "ewma_vol", None)
            vol_val = vol_per_sec.get_short_vol_per_sec() if vol_per_sec else 0.0001
            if vol_val < 0.0001:
                vol_regime = "low"
            elif vol_val < 0.0003:
                vol_regime = "medium"
            else:
                vol_regime = "high"

            is_win = (win.winner == settled_pos.side)
            bt_trade = BacktestTrade(
                slug=win.slug,
                side=settled_pos.side,
                action="BUY",
                order_type=last_signal.order_type,
                entry_price=settled_pos.avg_entry_price,
                shares=settled_pos.shares,
                cost_usd=settled_pos.total_cost_usd,
                fee_usd=settled_pos.total_fees_usd,
                elapsed_sec=last_signal.elapsed_sec,
                model_prob=last_signal.model_prob,
                expected_edge=last_signal.expected_edge,
                z_score=getattr(last_signal, "z_score", 0.0),
                vol_regime=vol_regime,
                hour_of_day=hour_of_day,
                winner=win.winner,
                is_win=is_win,
                payout_usd=settled_pos.payout_usd,
                pnl_usd=settled_pos.realized_pnl_usd,
            )
            window_trades.append(bt_trade)

        return window_trades

    def _apply_market_event(
        self,
        raw: Any,
        books: Dict[str, TokenOrderBook],
        token_to_side: Dict[str, str],
    ) -> None:
        if isinstance(raw, list):
            # Snapshot
            for item in raw:
                aid = item.get("asset_id")
                side = token_to_side.get(aid)
                if side and side in books:
                    books[side].apply_snapshot(item.get("bids", []), item.get("asks", []))
        elif isinstance(raw, dict):
            # Delta
            for chg in raw.get("price_changes", []):
                aid = chg.get("asset_id")
                side = token_to_side.get(aid)
                if side and side in books:
                    p = float(chg.get("price", 0.0))
                    s = float(chg.get("size", 0.0))
                    books[side].apply_delta(p, s, chg.get("side", ""))
