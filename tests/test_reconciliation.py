"""
Unit tests for Capital and Exchange Collateral Reconciliation Engine (Roadmap Phase 7).
Verifies:
1. Matching internal and external balances produces status="OK" and unblocks trading.
2. Balance discrepancies produce status="MISMATCH" and block new orders immediately.
3. Locked collateral from open positions and pending orders correctly reduces spendable active capital.
4. Reconciliation records are durably persisted in SQLite audit table.
5. Reconciliation adjustments are audited and never touch or drain protected capital to paper over losses.
"""
from __future__ import annotations

import time
from pathlib import Path
import pytest

from polybot.capital import (
    CapitalManager,
    CapitalReconciler,
    ExchangeBalanceSnapshot,
    ReconciliationReport,
)
from polybot.execution.base import Order, Position
from polybot.execution.paper import PaperExecutionEngine


@pytest.fixture
def clean_ledger_db(tmp_path: Path):
    db_file = tmp_path / "test_recon_ledger.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    yield mgr, db_file


def test_reconciliation_exact_match(clean_ledger_db):
    mgr, _ = clean_ledger_db
    reconciler = CapitalReconciler(mgr)

    snapshot = ExchangeBalanceSnapshot(available_collateral_cents=1100, locked_collateral_cents=0)
    report = reconciler.reconcile(external_snapshot=snapshot, trading_day="2026-11-01")

    assert report.is_valid is True
    assert report.status == "OK"
    assert report.discrepancy_cents == 0
    assert report.expected_equity_cents == 1100
    assert report.external_equity_cents == 1100
    assert report.spendable_active_cents == 1100
    assert mgr.state.reconciliation_blocked is False
    assert mgr.state.last_reconciliation_status == "OK"

    # Trade budgets should be unblocked
    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 1
    assert rev_b == 10


def test_reconciliation_discrepancy_blocks_trading(clean_ledger_db):
    mgr, _ = clean_ledger_db
    reconciler = CapitalReconciler(mgr)

    # External has $10.00 (1000 cents) while internal expects $11.00 (1100 cents)
    snapshot = ExchangeBalanceSnapshot(available_collateral_cents=1000, locked_collateral_cents=0)
    report = reconciler.reconcile(external_snapshot=snapshot, trading_day="2026-11-01")

    assert report.is_valid is False
    assert report.status == "MISMATCH"
    assert report.discrepancy_cents == -100
    assert mgr.state.reconciliation_blocked is True
    assert mgr.state.last_reconciliation_status == "MISMATCH"
    assert mgr.state.last_reconciliation_diff_cents == -100

    # Trade budgets must be $0 while reconciliation discrepancy is active
    blocked, reason = mgr.is_trading_blocked()
    assert blocked is True
    assert "Reconciliation discrepancy" in reason
    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 0
    assert rev_b == 0


def test_reconciliation_locked_collateral_calculation(clean_ledger_db):
    mgr, _ = clean_ledger_db
    engine = PaperExecutionEngine()

    # Simulate an open position of $2.00
    open_pos = Position(
        slug="btc-test-window",
        side="Up",
        shares=2.5,
        avg_entry_price=0.80,
        total_cost_usd=2.00,
        total_fees_usd=0.02,
        opened_at_ts=time.time(),
        status="OPEN",
    )
    engine.open_positions_list.append(open_pos)

    # Simulate a pending unfilled order of $1.00
    pending_order = Order(
        order_id="pending-123",
        slug="btc-test-window-2",
        side="Down",
        action="BUY",
        order_type="taker",
        price=0.85,
        usd_amount=1.00,
        shares=1.17,
        created_at_ts=time.time(),
        status="PENDING",
    )
    engine.pending_orders.append(pending_order)

    reconciler = CapitalReconciler(mgr, engine=engine)

    # External equity: $8.00 available + $3.00 locked = $11.00
    snapshot = ExchangeBalanceSnapshot(available_collateral_cents=800, locked_collateral_cents=300)
    report = reconciler.reconcile(external_snapshot=snapshot, trading_day="2026-11-01")

    assert report.is_valid is True
    assert report.locked_collateral_cents == 300  # $2 open pos + $1 pending order
    assert report.spendable_active_cents == 800   # $11 active - $3 locked = $8 spendable
    assert report.spendable_active_usd == 8.00
    assert report.locked_collateral_usd == 3.00


def test_reconciliation_audit_persistence(clean_ledger_db):
    mgr, _ = clean_ledger_db
    reconciler = CapitalReconciler(mgr)

    snapshot = ExchangeBalanceSnapshot(available_collateral_cents=1100, locked_collateral_cents=0)
    report = reconciler.reconcile(external_snapshot=snapshot, trading_day="2026-11-01")

    # Verify audit record in SQLite
    records = mgr.ledger.get_recent_reconciliations(limit=10)
    assert len(records) >= 1
    latest = records[0]
    assert latest["recon_id"] == report.recon_id
    assert latest["status"] == "OK"
    assert latest["expected_equity_cents"] == 1100
    assert latest["external_equity_cents"] == 1100
    assert latest["discrepancy_cents"] == 0

    last_record = mgr.ledger.get_last_reconciliation()
    assert last_record is not None
    assert last_record["recon_id"] == report.recon_id


def test_reconciliation_adjustment_never_drains_protected_capital(clean_ledger_db):
    mgr, _ = clean_ledger_db

    # First bring equity to $44 to activate milestone and create protected capital
    mgr.process_settlement(
        settlement_id="s-ms44",
        net_pnl_usd=33.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )
    assert mgr.state.milestone_44_activated is True
    assert mgr.state.active_capital == 22.0
    assert mgr.state.protected_capital == 22.0
    assert mgr.state.total_equity == 44.0

    reconciler = CapitalReconciler(mgr)

    # Perform an explicit deposit adjustment of +$5.00
    tx = reconciler.record_reconciliation_adjustment(
        amount_usd=5.0,
        reason="Manual bank wire deposit",
        trading_day="2026-11-01",
    )

    assert tx.amount_cents == 500
    assert mgr.state.active_capital == 27.0
    # Protected capital must remain completely untouched!
    assert mgr.state.protected_capital == 22.0
    assert mgr.state.total_equity == 49.0

    # Invariants must remain valid
    valid, errs = mgr.verify_invariants()
    assert valid is True
    assert len(errs) == 0


def test_reconciliation_negative_active_adjustment_rejected(clean_ledger_db):
    mgr, _ = clean_ledger_db
    reconciler = CapitalReconciler(mgr)

    # Attempting to deduct more than active capital via adjustment raises ValueError
    with pytest.raises(ValueError, match="negative active capital"):
        reconciler.record_reconciliation_adjustment(
            amount_usd=-20.0,
            reason="Illegal withdrawal attempt exceeding balance",
            trading_day="2026-11-01",
        )
