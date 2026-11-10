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
    Supports py-clob-client-v2 (CLOB API v2 / CTF Exchange v2) with fallback to legacy v1.
    """
    cfg = config or PolybotConfig.load()
    pk = os.getenv("POLY_PRIVATE_KEY")
    if not pk:
        raise ValueError("POLY_PRIVATE_KEY is required for live CLOB execution.")

    funder = os.getenv("POLY_FUNDER_ADDRESS")
    sig_type = int(os.getenv("POLY_SIG_TYPE", "3"))
    host = os.getenv("CLOB_REST_URL", "https://clob.polymarket.com")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    try:
        from py_clob_client_v2.client import ClobClient
        client = ClobClient(
            host=host,
            key=pk,
            chain_id=chain_id,
            signature_type=sig_type,
            funder=funder,
        )
        try:
            creds = client.derive_api_key()
        except Exception:
            creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        log.info("Live py-clob-client-v2 initialized and authenticated (sig_type=%d, funder=%s)", sig_type, funder)
        return client
    except ImportError:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            host=host,
            key=pk,
            chain_id=chain_id,
            signature_type=sig_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        log.info("Live py-clob-client (v1 legacy) initialized and authenticated")
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
        cap_val = self.live_config.micro_stake_cap_usd if self.live_config else None
        if cap_val is None:
            cap_val = float(os.getenv("MICRO_STAKE_CAP_USD", "75.0"))
        self.micro_stake_cap_usd = float(cap_val)
        log.info("Live trading safety cap: stake hard-capped at $%.2f", self.micro_stake_cap_usd)

        self.clob_client = clob_client
        self.ledger = ledger or TradeLedger(self.config.recorder.data_dir / "live_ledger.db")
        self.ledger_path = ledger_path

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

        self._cached_balance: float = 0.0
        self._last_balance_ts: float = 0.0

        # Reconcile on startup
        self.reconcile_on_startup()

    def get_wallet_balance(self, force_refresh: bool = False) -> float:
        """Queries Polymarket CLOB for current available collateral balance in USD.
        Implements a 500ms cache so polling every 200ms is sub-millisecond fast
        while keeping balance virtually real-time.
        """
        now = time.time()
        if not force_refresh and (now - self._last_balance_ts < 0.5):
            return self._cached_balance

        if not self.clob_client:
            return self._cached_balance

        try:
            bal_usd = 0.0
            if hasattr(self.clob_client, "get_balance_allowance"):
                try:
                    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
                except ImportError:
                    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                bal_data = self.clob_client.get_balance_allowance(params)
                if isinstance(bal_data, dict):
                    raw_bal = float(bal_data.get("balance", "0"))
                    bal_usd = raw_bal / 1e6

            # Fallback to on-chain Polygon check if CLOB reports 0.0 and funder address is configured
            if bal_usd <= 0.0:
                funder = os.getenv("POLY_FUNDER_ADDRESS")
                if funder:
                    on_chain = self._fetch_on_chain_balance(funder)
                    if on_chain > bal_usd:
                        bal_usd = on_chain

            self._cached_balance = bal_usd
            self._last_balance_ts = now
            return bal_usd
        except Exception as e:
            log.warning("Failed to fetch wallet collateral balance: %s", e)
            return self._cached_balance

    def _fetch_on_chain_balance(self, address: str) -> float:
        """Fallback check for on-chain USDC / USDC.e balance on Polygon."""
        try:
            import urllib.request
            import json
            rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
            clean_addr = address.lower().replace("0x", "")
            pad_addr = "000000000000000000000000" + clean_addr
            payload = json.dumps({
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [
                    {"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "data": f"0x70a08231{pad_addr}"},
                    "latest"
                ],
                "id": 1
            }).encode("utf-8")
            req = urllib.request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                val_hex = res.get("result", "0x0")
                return int(val_hex, 16) / 1e6
        except Exception:
            return 0.0

    def reconcile_on_startup(self) -> None:
        """
        Reconciles open positions and checks wallet balance on startup.
        """
        log.info("Performing live startup position and balance reconciliation...")
        if self.ledger_path and Path(self.ledger_path).exists():
            try:
                import json
                with open(self.ledger_path, "r") as f:
                    data = json.load(f)
                expected_positions = data.get("token_balances", {})
                if self.clob_client and hasattr(self.clob_client, "get_positions"):
                    server_positions = self.clob_client.get_positions() or {}
                    for token, exp_qty in expected_positions.items():
                        srv_qty = server_positions.get(token, 0.0)
                        if abs(exp_qty - srv_qty) > 1e-4:
                            raise RuntimeError(
                                f"POSITION RECONCILIATION MISMATCH for token {token}: "
                                f"Ledger={exp_qty}, Server={srv_qty}. Failing closed."
                            )
            except RuntimeError:
                raise
            except Exception as e:
                log.warning("Startup ledger reconciliation warning: %s", e)

        if self.clob_client:
            try:
                bal = self.get_wallet_balance(force_refresh=True)
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
            if hasattr(self.clob_client, "create_and_post_order"):
                order.token_id = f"tok-{order.slug}-{order.side}"
            else:
                order.status = "REJECTED"
                msg = f"Live order submission failed: Missing token_id for slug {order.slug} side {order.side}"
                log.error(msg)
                if self.on_order_rejected:
                    self.on_order_rejected(order, msg)
                return order

        # Determine order side and share sizing
        try:
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError:
            from py_clob_client.order_builder.constants import BUY, SELL

        import math
        side_str = BUY if order.action == "BUY" else SELL

        if side_str == BUY:
            # Polymarket CLOB enforces a strict minimum floor of $1.00 USD on marketable BUY orders.
            if order.usd_amount < 1.00:
                order.usd_amount = 1.00
            order.usd_amount = round(order.usd_amount, 2)
            if order.price > 0:
                order.shares = round(order.usd_amount / order.price, 4)
        else:
            order.shares = round(order.shares, 2)
            if order.price > 0 and order.usd_amount <= 0:
                order.usd_amount = round(order.shares * order.price, 4)

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
            try:
                from py_clob_client_v2.clob_types import MarketOrderArgs, OrderArgs, OrderType
            except ImportError:
                try:
                    from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
                except ImportError:
                    from py_clob_client.clob_types import OrderArgs, OrderType
                    MarketOrderArgs = None

            resp = None
            is_taker = getattr(order, "order_type", "taker") == "taker"

            # 1. Preferred path: use native MarketOrderArgs with FAK for taker snipe orders.
            # In Polymarket CLOB, market BUY orders require makerAmount to have at most 2 decimals (exact cents in USDC),
            # and takerAmount up to 4 decimals (shares). MarketOrderArgs accepts dollar cash budget for BUY
            # and correctly formats maker/taker token decimals to satisfy exchange precision validators.
            if is_taker and MarketOrderArgs is not None and hasattr(self.clob_client, "create_and_post_market_order"):
                market_amount = round(order.usd_amount, 2) if side_str == BUY else round(order.shares, 2)
                if side_str == BUY and market_amount < 1.00:
                    market_amount = 1.00
                market_args = MarketOrderArgs(
                    token_id=order.token_id,
                    amount=market_amount,
                    side=side_str,
                    price=round(order.price, 3),
                    order_type=OrderType.FAK,
                )
                log.info(
                    "[LIVE_MARKET_ORDER_POSTING] Submitting FAK market order: side=%s token=%s price=%.3f amount=%.2f (%s)",
                    side_str, order.token_id, order.price, market_amount, "USD" if side_str == BUY else "shares"
                )
                resp = self.clob_client.create_and_post_market_order(market_args, order_type=OrderType.FAK)

            # 2. Limit order path (GTC maker or fallback when client lacks create_and_post_market_order)
            if resp is None:
                clob_size = round(order.shares, 2) + 1e-6
                order_args = OrderArgs(
                    price=round(order.price, 3),
                    size=clob_size,
                    side=side_str,
                    token_id=order.token_id,
                )
                log.info(
                    "[LIVE_LIMIT_ORDER_POSTING] Submitting order: side=%s token=%s price=%.3f size=%.2f ($%.2f)",
                    side_str, order.token_id, order.price, order.shares, order.usd_amount
                )
                clob_order_type = OrderType.GTC
                try:
                    resp = self.clob_client.create_and_post_order(order_args, order_type=clob_order_type)
                except TypeError:
                    try:
                        resp = self.clob_client.create_and_post_order(order_args)
                    except TypeError:
                        resp = self.clob_client.create_and_post_order(
                            token_id=order.token_id,
                            price=round(order.price, 3),
                            side=side_str,
                            size=order.shares,
                            order_type=order.order_type,
                        )

            log.info("[LIVE_ORDER_RESPONSE] %s", resp)

            order_id = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
            if not order_id and isinstance(resp, dict):
                err = resp.get("errorMsg") or resp.get("error")
                if err:
                    raise RuntimeError(f"CLOB order rejected: {err}")

            order.order_id = order_id or f"live-{int(time.time() * 1000)}"
            order.status = "FILLED"

            # Parse exact filled taking/making amounts if provided
            if isinstance(resp, dict):
                taking_amt = float(resp.get("takingAmount") or resp.get("taking_amount") or 0.0)
                making_amt = float(resp.get("makingAmount") or resp.get("making_amount") or 0.0)
                if side_str == BUY:
                    if taking_amt > 0:
                        order.shares = taking_amt
                    if making_amt > 0:
                        order.usd_amount = making_amt
                elif side_str == SELL:
                    if making_amt > 0:
                        order.shares = making_amt
                    if taking_amt > 0:
                        order.usd_amount = taking_amt

            fee_usd = calculate_fee(order.usd_amount, self.fee_schedule.taker_rate)
            now_ts = time.time()
            fill = Fill(
                fill_id=str(uuid.uuid4())[:8],
                order_id=order.order_id,
                slug=order.slug,
                side=order.side,
                action=order.action,
                price=order.price,
                shares=order.shares,
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
            if hasattr(self.clob_client, "cancel_order"):
                res = self.clob_client.cancel_order(order_id)
            else:
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
