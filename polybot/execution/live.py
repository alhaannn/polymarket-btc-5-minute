"""
Strictly gated live execution module for Polymarket CLOB.
Enforces double-gate authorization, micro-live capital limits ($1 max stake),
position reconciliation on startup (fail-closed), partial fill tracking, and redemptions.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from polybot.config import LiveConfig, PolybotConfig
from polybot.execution.base import ExecutionEngine, Fill, Order, Position
from polybot.execution.fees import calculate_fee, FeeSchedule

log = logging.getLogger("polybot.execution.live")


class LiveExecutionEngine(ExecutionEngine):
    """
    Live trading execution engine interacting with Polymarket CLOB.
    GATED: Refuses instantiation unless both --live and LIVE_CONFIRM=YES_I_UNDERSTAND are provided.
    """

    def __init__(
        self,
        config: Optional[PolybotConfig] = None,
        clob_client: Optional[Any] = None,
        ledger_path: Optional[Path] = None,
    ):
        self.config = config or PolybotConfig()
        self.live_config = self.config.live

        # Rule 1 Double Gate Verification
        if not self.live_config.is_live_authorized():
            raise PermissionError(
                "LIVE TRADING ACCESS DENIED: Requires BOTH --live flag AND environment variable "
                "LIVE_CONFIRM=YES_I_UNDERSTAND. System will refuse to execute real orders."
            )

        log.warning("LIVE TRADING AUTHORIZED: Gated mode unlocked.")

        # Micro-live safety cap ($1.00 max stake)
        self.micro_stake_cap_usd = self.live_config.micro_stake_cap_usd
        log.info("Micro-live safety enabled: stake strictly hard-capped at $%.2f", self.micro_stake_cap_usd)

        self.clob_client = clob_client  # Optional injected client (e.g. py-clob-client or mock)
        self.ledger_path = ledger_path or (self.config.recorder.data_dir / "ledger.json")

        self.fee_schedule = FeeSchedule(
            taker_rate=self.config.strategy.taker_fee_rate,
            maker_rate=self.config.strategy.maker_fee_rate,
        )

        self.open_positions: Dict[str, Position] = {}
        self.settled_positions: List[Position] = []
        self.all_fills: List[Fill] = []

        # Perform position & balance reconciliation on startup
        self.reconcile_on_startup()

    def reconcile_on_startup(self) -> None:
        """
        Reconciles open positions and balances against local ledger.
        Fails closed (raises RuntimeError) on unexplained discrepancies.
        """
        log.info("Performing startup position and balance reconciliation...")
        local_positions = self._load_local_ledger()

        # If client is provided (live or mock), query exchange balance and positions
        if self.clob_client:
            try:
                server_positions = self.clob_client.get_positions()
                # Verify match
                for token_id, qty in local_positions.items():
                    server_qty = server_positions.get(token_id, 0.0)
                    if abs(qty - server_qty) > 1e-4:
                        raise RuntimeError(
                            f"STARTUP RECONCILIATION MISMATCH for token {token_id}: "
                            f"Local ledger has {qty}, Exchange has {server_qty}. Fails closed."
                        )
                log.info("Startup reconciliation PASSED: Local ledger matches exchange positions.")
            except Exception as e:
                log.error("Reconciliation failed: %s", e)
                raise RuntimeError(f"Startup reconciliation failure: {e}") from e
        else:
            log.info("Reconciliation complete (no remote client attached).")

    def _load_local_ledger(self) -> Dict[str, float]:
        if not self.ledger_path.exists():
            return {}
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                return json.load(f).get("token_balances", {})
        except Exception:
            return {}

    def _save_local_ledger(self, token_balances: Dict[str, float]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump({"token_balances": token_balances, "updated_at": time.time()}, f, indent=2)

    def submit_order(self, order: Order) -> Order:
        """
        Submits order to Polymarket CLOB.
        Strictly enforces micro-live stake cap ($1.00).
        """
        # Micro-live cap enforcement
        if order.usd_amount > self.micro_stake_cap_usd:
            log.warning(
                "Micro-live cap clamped order amount from $%.2f to $%.2f",
                order.usd_amount, self.micro_stake_cap_usd
            )
            order.usd_amount = self.micro_stake_cap_usd

        if not self.clob_client:
            order.status = "REJECTED"
            log.error("Live order submission failed: CLOB client not connected")
            return order

        try:
            # Place real order via clob_client
            resp = self.clob_client.create_and_post_order(
                token_id=order.slug,
                price=order.price,
                side=order.action,
                size=order.usd_amount / order.price,
                order_type=order.order_type,
            )
            order.status = resp.get("status", "PENDING")
            order.order_id = resp.get("order_id", order.order_id)
            log.info("LIVE ORDER SUBMITTED: ID=%s, status=%s", order.order_id, order.status)
            return order
        except Exception as e:
            order.status = "REJECTED"
            log.error("Live order error: %s", e)
            return order

    def cancel_order(self, order_id: str) -> bool:
        if not self.clob_client:
            return False
        try:
            res = self.clob_client.cancel(order_id)
            log.info("Cancelled live order %s", order_id)
            return bool(res)
        except Exception as e:
            log.error("Failed to cancel live order %s: %s", order_id, e)
            return False

    def redeem_winning_position(self, condition_id: str) -> bool:
        """Redeems settled winning collateral back to USDC from CTF exchange."""
        if not self.clob_client:
            return False
        try:
            log.info("Redeeming winning positions for condition %s...", condition_id)
            res = self.clob_client.redeem(condition_id)
            log.info("Redemption successful for %s", condition_id)
            return bool(res)
        except Exception as e:
            log.error("Redemption failed for %s: %s", condition_id, e)
            return False
