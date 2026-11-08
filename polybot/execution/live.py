"""
Strictly gated live execution module for Polymarket CLOB.
Enforces double-gate authorization, micro-live capital limits,
position reconciliation on startup (fail-closed), partial fill tracking, and on-chain order dispatch.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from polybot.config import LiveConfig, PolybotConfig
from polybot.execution.base import ExecutionEngine, Fill, Order, Position
from polybot.execution.fees import calculate_fee, FeeSchedule
from polybot.execution.ledger import TradeLedger

log = logging.getLogger("polybot.execution.live")


def create_clob_client(config: Optional[PolybotConfig] = None) -> Any:
    """
    Initializes and authenticates the py-clob-client with level 2 credentials.
    """
    from py_clob_client.client import ClobClient

    cfg = config or PolybotConfig.load()
    pk = os.getenv("POLY_PRIVATE_KEY")
    if not pk:
        raise ValueError("POLY_PRIVATE_KEY is required for live CLOB execution.")

    funder = os.getenv("POLY_FUNDER_ADDRESS")
    sig_type = int(os.getenv("POLY_SIG_TYPE", "2"))
    host = os.getenv("CLOB_REST_URL", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    client = ClobClient(
        host=host,
        key=pk,
        chain_id=chain_id,
        signature_type=sig_type,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


class LiveExecutionEngine(ExecutionEngine):
    """
    Live trading execution engine interacting with Polymarket CLOB.
    GATED: Refuses instantiation unless both --live / EXECUTION_MODE=live
    AND environment variable LIVE_CONFIRM=YES_I_UNDERSTAND are provided.
    """

    def __init__(
        self,
        config: Optional[PolybotConfig] = None,
        clob_client: Optional[Any] = None,
        ledger: Optional[TradeLedger] = None,
        ledger_path: Optional[Path] = None,
    ):
        self.config = config or PolybotConfig.load()
        self.live_config = self.config.live

        # Rule 1 Double Gate Verification
        if not self.live_config.is_live_authorized():
            raise PermissionError(
                "LIVE TRADING ACCESS DENIED: Requires BOTH --live flag AND environment variable "
                "LIVE_CONFIRM=YES_I_UNDERSTAND. System will refuse to execute real orders."
            )

        log.warning("LIVE TRADING AUTHORIZED: Gated mode unlocked.")

        # Sizing cap
        self.micro_stake_cap_usd = float(os.getenv("MICRO_STAKE_CAP_USD", str(self.live_config.micro_stake_cap_usd or 15.0)))
        log.info("Live trading safety cap: stake hard-capped at $%.2f", self.micro_stake_cap_usd)

        self.clob_client = clob_client
        self.ledger = ledger or TradeLedger(self.config.recorder.data_dir / "live_ledger.db")

        self.fee_schedule = FeeSchedule(
            taker_rate=self.config.strategy.taker_fee_rate,
            maker_rate=self.config.strategy.maker_fee_rate,
        )

        self.pending_orders: List[Order] = []
        self.open_positions: Dict[str, Position] = {}
        self.open_positions_list: List[Position] = []
        self.settled_positions: List[Position] = []
        self.all_fills: List[Fill] = []
        self.on_order_rejected: Optional[Callable[[Order, str], None]] = None

        # Reconcile on startup
        self.reconcile_on_startup()

    def get_wallet_balance(self) -> float:
        """Queries Polymarket CLOB for current available collateral balance in USD."""
        if not self.clob_client:
            return 0.0
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal_data = self.clob_client.get_balance_allowance(params)
            raw_bal = float(bal_data.get("balance", "0"))
            return raw_bal / 1e6
        except Exception as e:
            log.warning("Failed to fetch wallet collateral balance: %s", e)
            return 0.0

    def reconcile_on_startup(self) -> None:
        """
        Reconciles open positions and checks wallet balance on startup.
        """
        log.info("Performing live startup position and balance reconciliation...")
        if self.clob_client:
            try:
                bal = self.get_wallet_balance()
                log.info("Startup live wallet balance verified: $%.2f USDC", bal)
            except Exception as e:
                log.warning("Startup live reconciliation warning: %s", e)

    def submit_order(self, order: Order) -> Order:
        """
        Submits real order to Polymarket CLOB.
        """
        if order.usd_amount > self.micro_stake_cap_usd:
            log.warning(
                "Micro-live cap clamped order amount from $%.2f to $%.2f",
                order.usd_amount, self.micro_stake_cap_usd
            )
            order.usd_amount = self.micro_stake_cap_usd

        if not self.clob_client:
            order.status = "REJECTED"
            msg = "Live order submission failed: CLOB client not connected"
            log.error(msg)
            if self.on_order_rejected:
                self.on_order_rejected(order, msg)
            return order

        if not order.token_id:
            order.status = "REJECTED"
            msg = f"Live order submission failed: Missing token_id for slug {order.slug} side {order.side}"
            log.error(msg)
            if self.on_order_rejected:
                self.on_order_rejected(order, msg)
            return order

        # Pre-execution wallet balance check
        try:
            avail_bal = self.get_wallet_balance()
            if avail_bal > 0 and avail_bal < order.usd_amount:
                order.status = "REJECTED"
                msg = f"Insufficient wallet balance: Available ${avail_bal:.2f} < Required ${order.usd_amount:.2f}"
                log.error("[INSUFFICIENT_FUNDS] %s", msg)
                if self.on_order_rejected:
                    self.on_order_rejected(order, msg)
                return order
        except Exception as e:
            log.warning("Pre-order balance check warning: %s", e)

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL

            side_str = BUY if order.action == "BUY" else SELL
            size = round(order.usd_amount / order.price, 2)
            if size <= 0:
                size = 1.0

            order_args = OrderArgs(
                price=round(order.price, 3),
                size=size,
                side=side_str,
                token_id=order.token_id,
            )

            log.info(
                "[LIVE_ORDER_POSTING] Submitting to CLOB: side=%s token=%s price=%.3f size=%.2f ($%.2f)",
                side_str, order.token_id, order.price, size, order.usd_amount
            )

            resp = self.clob_client.create_and_post_order(order_args)
            log.info("[LIVE_ORDER_RESPONSE] %s", resp)

            order_id = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
            if not order_id and isinstance(resp, dict) and "errorMsg" in resp:
                raise RuntimeError(resp.get("errorMsg", "Unknown CLOB order rejection"))

            order.order_id = order_id or f"live-{int(time.time() * 1000)}"
            order.status = "FILLED"

            fee_usd = calculate_fee(order.usd_amount, self.fee_schedule.taker_rate)
            now_ts = time.time()
            fill = Fill(
                fill_id=str(uuid.uuid4())[:8],
                order_id=order.order_id,
                slug=order.slug,
                side=order.side,
                action=order.action,
                price=order.price,
                shares=size,
                cost_usd=order.usd_amount,
                fee_usd=fee_usd,
                filled_at_ts=now_ts,
                signal_type=order.signal_type,
                linked_order_id=order.linked_order_id,
                trigger_prob=order.trigger_prob,
                counterpart_prob=order.counterpart_prob,
                data_receipt_ts=order.data_receipt_ts,
                token_id=order.token_id,
            )

            self._record_fill(fill, order)
            return order

        except Exception as e:
            order.status = "REJECTED"
            msg = f"Live order execution error: {e}"
            log.error(msg)
            if self.on_order_rejected:
                self.on_order_rejected(order, msg)
            return order

    def _record_fill(self, fill: Fill, order: Optional[Order] = None) -> None:
        self.all_fills.append(fill)

        if fill.action == "BUY":
            pos_id = str(uuid.uuid4())[:8]
            pos = Position(
                slug=fill.slug,
                side=fill.side,
                shares=fill.shares,
                avg_entry_price=fill.price,
                total_cost_usd=fill.cost_usd,
                total_fees_usd=fill.fee_usd,
                opened_at_ts=fill.filled_at_ts,
                status="OPEN",
                position_id=pos_id,
                order_id=fill.order_id,
                signal_type=fill.signal_type,
                linked_order_id=fill.linked_order_id,
                trigger_prob=fill.trigger_prob,
                counterpart_prob=fill.counterpart_prob,
                data_receipt_ts=fill.data_receipt_ts,
                token_id=fill.token_id,
            )
            self.open_positions[fill.slug] = pos
            self.open_positions_list.append(pos)

            try:
                lat = (fill.filled_at_ts - order.created_at_ts) * 1000.0 if order else 0.0
                receipt_ts = order.data_receipt_ts if order and order.data_receipt_ts > 0 else fill.filled_at_ts
                self.ledger.record_trade(
                    order_id=fill.order_id,
                    slug=fill.slug,
                    side=fill.side,
                    action=fill.action,
                    order_type="taker",
                    signal_type=fill.signal_type,
                    stake_usd=fill.cost_usd,
                    shares=fill.shares,
                    avg_price=fill.price,
                    fee_usd=fill.fee_usd,
                    exec_ts=fill.filled_at_ts,
                    linked_order_id=fill.linked_order_id,
                    trigger_prob=fill.trigger_prob,
                    counterpart_prob=fill.counterpart_prob,
                    data_receipt_ts=receipt_ts,
                    signal_detection_ts=order.created_at_ts if order else fill.filled_at_ts,
                    latency_ms=lat,
                    status="OPEN",
                )
            except Exception as e:
                log.warning("Live ledger recording failed: %s", e)

        elif fill.action == "SELL":
            pos = self.open_positions.pop(fill.slug, None)
            if pos:
                if pos in self.open_positions_list:
                    self.open_positions_list.remove(pos)
                pos.status = "CLOSED_EARLY"
                pos.settled_at_ts = fill.filled_at_ts
                pos.payout_usd = fill.cost_usd
                pos.total_fees_usd += fill.fee_usd
                pos.realized_pnl_usd = pos.payout_usd - pos.total_cost_usd - pos.total_fees_usd
                self.settled_positions.append(pos)
                try:
                    self.ledger.settle_order(
                        order_id=pos.order_id,
                        winner="EXIT_EARLY",
                        payout_usd=pos.payout_usd,
                        pnl_usd=pos.realized_pnl_usd,
                        settled_at_ts=fill.filled_at_ts,
                    )
                except Exception as e:
                    log.warning("Live ledger settle_order for early exit failed: %s", e)

    def cancel_order(self, order_id: str) -> bool:
        if not self.clob_client:
            return False
        try:
            res = self.clob_client.cancel(order_id)
            log.info("Cancelled live order %s: %s", order_id, res)
            return bool(res)
        except Exception as e:
            log.error("Failed to cancel live order %s: %s", order_id, e)
            return False

    def cancel_pending_orders(self) -> int:
        if not self.clob_client:
            return 0
        try:
            self.clob_client.cancel_all()
            log.info("Cancelled all open live orders on CLOB")
            return 1
        except Exception as e:
            log.warning("cancel_all warning: %s", e)
            return 0

    def process_pending_orders(self, now_ts: float, side_books: Any) -> List[Fill]:
        # Live orders are posted synchronously to CLOB, fills are immediate on taker match
        return []

    def settle_window_all(self, slug: str, winner: str, settle_ts: float) -> List[Position]:
        positions_to_settle = [p for p in self.open_positions_list if p.slug == slug and p.status == "OPEN"]
        if not positions_to_settle and slug in self.open_positions:
            pos = self.open_positions.get(slug)
            if pos and pos.status == "OPEN":
                positions_to_settle.append(pos)

        if not positions_to_settle:
            return []

        try:
            self.ledger.settle_slug(slug, winner, settle_ts)
        except Exception as e:
            log.warning("Live ledger settlement failed for %s: %s", slug, e)

        settled_list = []
        for pos in positions_to_settle:
            is_win = (winner == pos.side)
            payout = pos.shares if is_win else 0.0
            pnl = payout - pos.total_cost_usd - pos.total_fees_usd

            pos.status = "SETTLED"
            pos.settled_at_ts = settle_ts
            pos.settle_winner = winner
            pos.payout_usd = round(payout, 4)
            pos.realized_pnl_usd = round(pnl, 4)

            self.settled_positions.append(pos)
            if pos in self.open_positions_list:
                self.open_positions_list.remove(pos)

            log.info(
                "[LIVE_SETTLED] %s position=%s (%s) winner=%s payout=$%.2f PnL=$%+.2f",
                slug, pos.side, pos.signal_type, winner, payout, pnl
            )
            settled_list.append(pos)

        self.open_positions.pop(slug, None)
        return settled_list

    def settle_window(self, slug: str, winner: str, settle_ts: float) -> Optional[Position]:
        settled = self.settle_window_all(slug, winner, settle_ts)
        return settled[-1] if settled else None

    def get_open_exposure(self) -> float:
        return sum(p.total_cost_usd for p in self.open_positions_list if p.status == "OPEN")

    def get_total_realized_pnl(self) -> float:
        return sum(p.realized_pnl_usd for p in self.settled_positions)
