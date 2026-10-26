"""
Realistic paper execution engine with latency simulation, taker/maker order types,
slippage checks, and fee deduction.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional, Tuple

from polybot.config import StrategyConfig
from polybot.execution.base import ExecutionEngine, Fill, Order, Position
from polybot.execution.fees import calculate_fee, FeeSchedule
from polybot.feeds.clob_ws import TokenOrderBook

log = logging.getLogger("polybot.execution.paper")


class PaperExecutionEngine(ExecutionEngine):
    """
    Simulates realistic order execution:
    - 400ms latency buffer (fills against book at t_decision + latency)
    - Taker order book walking with slippage guard
    - Maker limit order resting with trade-through fills and timeout cancellations
    - Complete portfolio position and PnL accounting
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.fee_schedule = FeeSchedule(
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
        )

        self.pending_orders: List[Order] = []
        self.open_positions: Dict[str, Position] = {}  # slug -> Position
        self.settled_positions: List[Position] = []
        self.all_fills: List[Fill] = []

    def submit_order(self, order: Order) -> Order:
        """Enqueue order into the pending execution queue."""
        order.status = "PENDING"
        self.pending_orders.append(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        for o in self.pending_orders:
            if o.order_id == order_id:
                o.status = "CANCELLED"
                self.pending_orders.remove(o)
                log.info("Cancelled pending order %s", order_id)
                return True
        return False

    def process_pending_orders(
        self,
        current_ts: float,
        books: Dict[str, TokenOrderBook],
    ) -> List[Fill]:
        """
        Process pending orders against current books after simulated latency.
        Called on every tick.
        """
        new_fills: List[Fill] = []

        for order in list(self.pending_orders):
            # Check latency: order must wait latency_ms before filling
            delay_sec = order.latency_ms / 1000.0
            if current_ts < order.created_at_ts + delay_sec:
                continue  # still in transit

            book = books.get(order.side)
            if not book:
                continue

            if order.order_type == "taker":
                fill = self._execute_taker(order, current_ts, book)
                if fill:
                    new_fills.append(fill)
                self.pending_orders.remove(order)

            elif order.order_type == "maker":
                fill, should_remove = self._evaluate_maker(order, current_ts, book)
                if fill:
                    new_fills.append(fill)
                if should_remove:
                    self.pending_orders.remove(order)

        return new_fills

    def _execute_taker(
        self,
        order: Order,
        current_ts: float,
        book: TokenOrderBook,
    ) -> Optional[Fill]:
        """Execute market/taker order walking the book."""
        if order.action == "BUY":
            sim = book.simulate_buy(order.usd_amount)
            if sim is None:
                order.status = "REJECTED"
                log.warning("Taker BUY rejected for %s %s: insufficient depth", order.slug, order.side)
                return None

            avg_price, shares, spent = sim
            best_ask = book.best_ask() or avg_price
            slippage = (avg_price - best_ask) / best_ask if best_ask > 0 else 0.0

            if slippage > self.config.max_slippage:
                order.status = "REJECTED"
                log.warning(
                    "Taker BUY rejected: slippage %.2f%% > max %.2f%%",
                    slippage * 100.0, self.config.max_slippage * 100.0
                )
                return None

            fee = calculate_fee(avg_price, shares, "taker", self.fee_schedule)
            fill = Fill(
                fill_id=str(uuid.uuid4())[:8],
                order_id=order.order_id,
                slug=order.slug,
                side=order.side,
                action="BUY",
                price=avg_price,
                shares=shares,
                cost_usd=spent,
                fee_usd=fee,
                filled_at_ts=current_ts,
            )
            order.status = "FILLED"
            self._record_fill(fill)
            log.info(
                "TAKER FILLED BUY %s %s @ %.3f x %.2f shares (cost=$%.2f, fee=$%.3f, lat=%dms)",
                order.slug, order.side, avg_price, shares, spent, fee, order.latency_ms
            )
            return fill

        elif order.action == "SELL":
            # Early exit sell
            sim = book.simulate_sell(order.shares)
            if sim is None:
                order.status = "REJECTED"
                log.warning("Taker SELL rejected: insufficient bid depth")
                return None

            avg_price, shares, received = sim
            fee = calculate_fee(avg_price, shares, "taker", self.fee_schedule)
            fill = Fill(
                fill_id=str(uuid.uuid4())[:8],
                order_id=order.order_id,
                slug=order.slug,
                side=order.side,
                action="SELL",
                price=avg_price,
                shares=shares,
                cost_usd=received,
                fee_usd=fee,
                filled_at_ts=current_ts,
            )
            order.status = "FILLED"
            self._record_fill(fill)
            log.info(
                "TAKER FILLED SELL %s %s @ %.3f x %.2f shares (rec=$%.2f, fee=$%.3f)",
                order.slug, order.side, avg_price, shares, received, fee
            )
            return fill

        return None

    def _evaluate_maker(
        self,
        order: Order,
        current_ts: float,
        book: TokenOrderBook,
    ) -> Tuple[Optional[Fill], bool]:
        """
        Evaluates resting maker limit order.
        Fills only if market trades through our price (best ask <= our limit price for BUY).
        Cancels after timeout.
        """
        elapsed = current_ts - order.created_at_ts
        if elapsed > self.config.maker_timeout_sec:
            order.status = "CANCELLED"
            log.info("Maker order %s timed out after %.1fs", order.order_id, elapsed)
            return None, True

        # For a BUY maker order resting at limit price:
        # Fills if current best ask <= order.price (sellers sold down into our bid)
        best_ask = book.best_ask()
        if best_ask is not None and best_ask <= order.price:
            shares = order.usd_amount / order.price
            spent = order.usd_amount
            fee = calculate_fee(order.price, shares, "maker", self.fee_schedule)
            fill = Fill(
                fill_id=str(uuid.uuid4())[:8],
                order_id=order.order_id,
                slug=order.slug,
                side=order.side,
                action="BUY",
                price=order.price,
                shares=shares,
                cost_usd=spent,
                fee_usd=fee,
                filled_at_ts=current_ts,
            )
            order.status = "FILLED"
            self._record_fill(fill)
            log.info(
                "MAKER FILLED BUY %s %s @ %.3f x %.2f shares (fee=$%.3f)",
                order.slug, order.side, order.price, shares, fee
            )
            return fill, True

        return None, False

    def _record_fill(self, fill: Fill) -> None:
        self.all_fills.append(fill)

        if fill.action == "BUY":
            pos = Position(
                slug=fill.slug,
                side=fill.side,
                shares=fill.shares,
                avg_entry_price=fill.price,
                total_cost_usd=fill.cost_usd,
                total_fees_usd=fill.fee_usd,
                opened_at_ts=fill.filled_at_ts,
                status="OPEN",
            )
            self.open_positions[fill.slug] = pos

        elif fill.action == "SELL":
            pos = self.open_positions.pop(fill.slug, None)
            if pos:
                pos.status = "CLOSED_EARLY"
                pos.settled_at_ts = fill.filled_at_ts
                pos.payout_usd = fill.cost_usd
                pos.total_fees_usd += fill.fee_usd
                pos.realized_pnl_usd = pos.payout_usd - pos.total_cost_usd - pos.total_fees_usd
                self.settled_positions.append(pos)

    def settle_window(self, slug: str, winner: str, settle_ts: float) -> Optional[Position]:
        """Settle an open position upon market resolution."""
        pos = self.open_positions.pop(slug, None)
        if not pos:
            return None

        is_win = (winner == pos.side)
        payout = pos.shares if is_win else 0.0
        pnl = payout - pos.total_cost_usd - pos.total_fees_usd

        pos.status = "SETTLED"
        pos.settled_at_ts = settle_ts
        pos.settle_winner = winner
        pos.payout_usd = round(payout, 4)
        pos.realized_pnl_usd = round(pnl, 4)

        self.settled_positions.append(pos)
        log.info(
            "SETTLED %s position=%s winner=%s payout=$%.2f PnL=$%+.2f",
            slug, pos.side, winner, payout, pnl
        )
        return pos

    def get_total_realized_pnl(self) -> float:
        return sum(p.realized_pnl_usd for p in self.settled_positions)
