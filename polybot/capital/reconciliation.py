"""
Capital and Exchange Collateral Reconciliation Engine.
Guarantees internal accounting does not drift silently from actual exchange
collateral, locked orders, and open position state.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from polybot.capital.manager import CapitalManager
from polybot.capital.models import CapitalTransaction, TransactionType

log = logging.getLogger("polybot.capital.reconciliation")


@dataclass
class ExchangeBalanceSnapshot:
    """External snapshot of exchange collateral and commitments in integer cents."""
    available_collateral_cents: int
    locked_collateral_cents: int = 0
    unrealized_pnl_cents: int = 0

    @property
    def total_equity_cents(self) -> int:
        return self.available_collateral_cents + self.locked_collateral_cents + self.unrealized_pnl_cents

    @property
    def total_equity_usd(self) -> float:
        return round(self.total_equity_cents / 100.0, 2)


@dataclass
class ReconciliationReport:
    """Comprehensive reconciliation report comparing internal vs external capital state."""
    recon_id: str
    is_valid: bool
    expected_equity_cents: int
    external_equity_cents: int
    locked_collateral_cents: int
    spendable_active_cents: int
    discrepancy_cents: int
    status: str  # "OK" or "MISMATCH"
    trading_day: str
    message: str
    timestamp: float

    @property
    def discrepancy_usd(self) -> float:
        return round(self.discrepancy_cents / 100.0, 2)

    @property
    def expected_equity_usd(self) -> float:
        return round(self.expected_equity_cents / 100.0, 2)

    @property
    def external_equity_usd(self) -> float:
        return round(self.external_equity_cents / 100.0, 2)

    @property
    def spendable_active_usd(self) -> float:
        return round(self.spendable_active_cents / 100.0, 2)

    @property
    def locked_collateral_usd(self) -> float:
        return round(self.locked_collateral_cents / 100.0, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recon_id": self.recon_id,
            "is_valid": self.is_valid,
            "expected_equity_cents": self.expected_equity_cents,
            "external_equity_cents": self.external_equity_cents,
            "locked_collateral_cents": self.locked_collateral_cents,
            "spendable_active_cents": self.spendable_active_cents,
            "discrepancy_cents": self.discrepancy_cents,
            "status": self.status,
            "trading_day": self.trading_day,
            "message": self.message,
            "timestamp": self.timestamp,
            "created_at_ts": self.timestamp,
            "expected_equity_usd": self.expected_equity_usd,
            "external_equity_usd": self.external_equity_usd,
            "spendable_active_usd": self.spendable_active_usd,
            "locked_collateral_usd": self.locked_collateral_usd,
            "discrepancy_usd": self.discrepancy_usd,
        }


class CapitalReconciler:
    """
    Automated auditor reconciling CapitalManager internal balances with
    execution engine open exposure, pending orders, and external exchange balances.
    """

    def __init__(
        self,
        capital_manager: CapitalManager,
        engine: Optional[Any] = None,
    ):
        self.capital_mgr = capital_manager
        self.engine = engine

    def reconcile(
        self,
        external_snapshot: Optional[ExchangeBalanceSnapshot] = None,
        trading_day: Optional[str] = None,
    ) -> ReconciliationReport:
        """
        Executes a formal reconciliation audit.
        If a discrepancy is detected:
        - Logs [CAPITAL_RECONCILIATION_MISMATCH]
        - Sets reconciliation_blocked = True on capital_state
        - Persists audit record in SQLite
        If reconciled:
        - Logs [CAPITAL_RECONCILIATION_COMPLETED]
        - Sets reconciliation_blocked = False
        """
        now = time.time()
        day_key = trading_day or time.strftime("%Y-%m-%d")
        recon_id = f"recon-{int(now * 1000)}-{uuid.uuid4().hex[:4]}"

        # 1. Determine locked collateral (open positions + pending unfilled orders)
        locked_cents = 0
        if self.engine is not None:
            if hasattr(self.engine, "get_open_exposure"):
                open_exp_usd = self.engine.get_open_exposure()
                locked_cents += int(round(open_exp_usd * 100))

            if hasattr(self.engine, "pending_orders"):
                for order in self.engine.pending_orders:
                    if getattr(order, "status", "") == "PENDING":
                        locked_cents += int(round(getattr(order, "usd_amount", 0.0) * 100))

        # 2. Derive expected equity and spendable active funds
        state = self.capital_mgr.state
        expected_equity_cents = state.total_equity_cents
        spendable_active_cents = max(0, state.active_capital_cents - locked_cents)

        # 3. Determine external equity to compare against
        if external_snapshot is not None:
            external_equity_cents = external_snapshot.total_equity_cents
        else:
            # Standalone paper verification: verify internal consistency
            invariants_ok, invariant_errs = self.capital_mgr.verify_invariants()
            if not invariants_ok:
                external_equity_cents = expected_equity_cents + 100  # Synthesize mismatch for invariant violation
                log.error("Internal invariant check failed during reconciliation: %s", invariant_errs)
            else:
                external_equity_cents = expected_equity_cents

        discrepancy_cents = external_equity_cents - expected_equity_cents
        is_valid = (discrepancy_cents == 0)

        if is_valid:
            status = "OK"
            message = (
                f"Reconciliation successful. Total Equity=${expected_equity_cents/100:.2f}, "
                f"Active=${state.active_capital:.2f} (Spendable=${spendable_active_cents/100:.2f}, "
                f"Locked=${locked_cents/100:.2f}), Protected=${state.protected_capital:.2f}."
            )
            state.reconciliation_blocked = False
            state.last_reconciliation_status = "OK"
            state.last_reconciliation_diff_cents = 0
            state.last_reconciliation_ts = now
            log.info("[CAPITAL_RECONCILIATION_COMPLETED] id=%s: %s", recon_id, message)
        else:
            status = "MISMATCH"
            message = (
                f"Discrepancy detected: Expected Total Equity ${expected_equity_cents/100:.2f}, "
                f"Found External Equity ${external_equity_cents/100:.2f} (Difference: ${discrepancy_cents/100:+.2f}). "
                f"New orders are strictly blocked."
            )
            state.reconciliation_blocked = True
            state.last_reconciliation_status = "MISMATCH"
            state.last_reconciliation_diff_cents = discrepancy_cents
            state.last_reconciliation_ts = now
            log.error("[CAPITAL_RECONCILIATION_MISMATCH] id=%s: %s", recon_id, message)

        report = ReconciliationReport(
            recon_id=recon_id,
            is_valid=is_valid,
            expected_equity_cents=expected_equity_cents,
            external_equity_cents=external_equity_cents,
            locked_collateral_cents=locked_cents,
            spendable_active_cents=spendable_active_cents,
            discrepancy_cents=discrepancy_cents,
            status=status,
            trading_day=day_key,
            message=message,
            timestamp=now,
        )

        # Persist audit record in SQLite
        try:
            self.capital_mgr.ledger.record_reconciliation(report.to_dict())
        except Exception as e:
            log.error("Failed to record reconciliation audit record: %s", e)

        return report

    def record_reconciliation_adjustment(
        self,
        amount_usd: float,
        reason: str,
        trading_day: Optional[str] = None,
    ) -> CapitalTransaction:
        """
        Records an explicit, auditable balance adjustment (e.g. deposit, external withdrawal,
        or correction). Never transfers protected capital to active to paper over trading losses.
        """
        now = time.time()
        day_key = trading_day or time.strftime("%Y-%m-%d")
        amount_cents = int(round(amount_usd * 100))

        state = self.capital_mgr.state
        active_before = state.active_capital_cents
        prot_before = state.protected_capital_cents
        tot_before = state.total_equity_cents

        new_active = active_before + amount_cents
        if new_active < 0:
            raise ValueError(f"Adjustment of ${amount_usd:.2f} would result in negative active capital (${new_active/100:.2f})")

        state.active_capital_cents = new_active
        state.total_equity_cents = new_active + prot_before

        tx = CapitalTransaction(
            tx_id=f"tx-adj-{int(now * 1000)}-{uuid.uuid4().hex[:4]}",
            event_type=TransactionType.RECONCILIATION_ADJUSTMENT,
            amount_cents=amount_cents,
            active_before_cents=active_before,
            active_after_cents=new_active,
            protected_before_cents=prot_before,
            protected_after_cents=prot_before,
            total_before_cents=tot_before,
            total_after_cents=state.total_equity_cents,
            fee_cents=0,
            trading_day=day_key,
            reason=f"Reconciliation Adjustment: {reason}",
            idempotency_key=f"adj-{int(now * 1000)}",
        )

        self.capital_mgr.ledger.save_state_and_transaction(state, tx)
        log.info(
            "Recorded reconciliation adjustment: Amount=$%+.2f | Active: $%.2f -> $%.2f | Total: $%.2f -> $%.2f",
            amount_usd,
            active_before / 100.0,
            new_active / 100.0,
            tot_before / 100.0,
            state.total_equity_cents / 100.0,
        )

        # Trigger immediate re-reconciliation
        self.reconcile(trading_day=day_key)
        return tx
