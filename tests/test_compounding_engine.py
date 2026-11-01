"""
Comprehensive Unit Tests for Phase 4: Capital Ledger & Compounding Engine.
Covers all Phase 4 Section G test cases:
1. Exact and overshoot $44 milestone activation (50/50 split)
2. Odd-cent milestone allocation policy
3. Post-milestone 50/50 profit compounding ($10 profit split)
4. Multiple sequential profit splits with zero penny drift
5. Odd-cent profit allocation policy (conservative protected-capital bias)
6. Active-only loss deduction (protected capital strictly untouched)
7. Active shortfall handling (loss exceeding active pool floors active at 0 and halts trading)
8. Shortfall recovery (subsequent profit clears deficit before compounding)
9. Settlement idempotency (duplicate settlement IDs safely ignored)
10. Late settlement handling (cross-day settlement attribution)
11. Fee-adjusted net P&L tracking
12. Crash resilience & atomic persistence in SQLite ledger
13. Reconciliation validation against external balances
"""
import pytest
import tempfile
from pathlib import Path

from polybot.capital.ledger import CapitalLedger
from polybot.capital.manager import CapitalManager
from polybot.capital.models import TransactionType


@pytest.fixture
def tmp_ledger(tmp_path: Path):
    db_path = tmp_path / "test_ledger.db"
    return CapitalLedger(db_path)


@pytest.fixture
def manager(tmp_ledger):
    return CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)


# --- 1. Milestone 44 Triggers ---

def test_milestone_44_exact_activation(manager):
    """$11 starting + $33 profit = $44.00 exact milestone trigger -> $22.00 Active / $22.00 Protected."""
    assert manager.state.active_capital == 11.0
    assert manager.state.protected_capital == 0.0
    assert not manager.state.milestone_44_activated

    manager.process_settlement(
        settlement_id="settle-ms-exact",
        net_pnl_usd=33.0,
        fee_usd=0.10,
        trading_day="2026-11-01"
    )

    assert manager.state.milestone_44_activated is True
    assert manager.state.total_equity == 44.0
    assert manager.state.active_capital == 22.0
    assert manager.state.protected_capital == 22.0
    assert manager.state.active_capital_cents == 2200
    assert manager.state.protected_capital_cents == 2200
    assert manager.state.total_equity_cents == 4400


def test_milestone_44_overshoot_activation(manager):
    """$11 starting + $34 profit = $45.00 total equity -> $22.50 Active / $22.50 Protected."""
    manager.process_settlement(
        settlement_id="settle-ms-over",
        net_pnl_usd=34.0,
        fee_usd=0.10,
        trading_day="2026-11-01"
    )

    assert manager.state.milestone_44_activated is True
    assert manager.state.total_equity == 45.0
    assert manager.state.active_capital == 22.50
    assert manager.state.protected_capital == 22.50
    assert manager.state.active_capital_cents == 2250
    assert manager.state.protected_capital_cents == 2250


def test_milestone_44_odd_cent_allocation(manager):
    """
    $11 starting + $33.01 profit = $44.01 total equity (4401 cents).
    Odd-cent policy: Active gets 4401 // 2 = 2200c ($22.00), Protected gets 2201c ($22.01).
    """
    manager.process_settlement(
        settlement_id="settle-ms-odd",
        net_pnl_usd=33.01,
        fee_usd=0.05,
        trading_day="2026-11-01"
    )

    assert manager.state.milestone_44_activated is True
    assert manager.state.total_equity_cents == 4401
    assert manager.state.active_capital_cents == 2200
    assert manager.state.protected_capital_cents == 2201
    assert manager.state.active_capital == 22.00
    assert manager.state.protected_capital == 22.01
    assert manager.state.total_equity == 44.01


# --- 2. Post-Milestone Profit Compounding ---

def test_post_milestone_10_dollar_profit_split(manager):
    """After reaching $44, a $10.00 profit splits +$5.00 Active / +$5.00 Protected."""
    # Reach $44.00
    manager.process_settlement("init-to-44", 33.0, trading_day="2026-11-01")
    assert manager.state.active_capital == 22.0
    assert manager.state.protected_capital == 22.0

    # Apply $10 profit
    manager.process_settlement("profit-10", 10.0, fee_usd=0.08, trading_day="2026-11-01")

    assert manager.state.total_equity == 54.0
    assert manager.state.active_capital == 27.0
    assert manager.state.protected_capital == 27.0
    assert manager.state.realized_profit == 43.0


def test_multiple_sequential_profit_splits(manager):
    """Sequential profits compound with exact cent conservation."""
    manager.process_settlement("reach-44", 33.0, trading_day="2026-11-01")

    profits = [2.00, 4.50, 8.20, 1.30]
    expected_active_cents = 2200
    expected_prot_cents = 2200

    for idx, p in enumerate(profits):
        p_cents = int(round(p * 100))
        act_share = p_cents // 2
        prot_share = p_cents - act_share
        expected_active_cents += act_share
        expected_prot_cents += prot_share

        manager.process_settlement(f"seq-prof-{idx}", p, trading_day="2026-11-01")

        assert manager.state.active_capital_cents == expected_active_cents
        assert manager.state.protected_capital_cents == expected_prot_cents
        assert manager.state.total_equity_cents == (expected_active_cents + expected_prot_cents)


def test_odd_cent_profit_allocation_policy(manager):
    """
    Post-milestone odd cent profit:
    $1.01 (101c) -> Active +50c ($0.50), Protected +51c ($0.51).
    $0.01 (1c) -> Active +0c ($0.00), Protected +1c ($0.01).
    """
    manager.process_settlement("reach-44", 33.0, trading_day="2026-11-01")

    # $1.01 profit
    manager.process_settlement("odd-101c", 1.01, trading_day="2026-11-01")
    assert manager.state.active_capital_cents == 2200 + 50
    assert manager.state.protected_capital_cents == 2200 + 51
    assert manager.state.total_equity_cents == 4400 + 101

    # $0.01 profit
    manager.process_settlement("odd-1c", 0.01, trading_day="2026-11-01")
    assert manager.state.active_capital_cents == 2250 + 0
    assert manager.state.protected_capital_cents == 2251 + 1
    assert manager.state.total_equity_cents == 4502


# --- 3. Active-Only Loss Deduction & Shortfall Handling ---

def test_active_only_loss_deduction(manager):
    """Realized losses are deducted exclusively from active capital; protected capital is strictly preserved."""
    manager.process_settlement("reach-44", 33.0, trading_day="2026-11-01")
    assert manager.state.active_capital == 22.0
    assert manager.state.protected_capital == 22.0

    # $8.00 loss
    manager.process_settlement("loss-8", -8.0, fee_usd=0.05, trading_day="2026-11-01")

    assert manager.state.active_capital == 14.0
    assert manager.state.protected_capital == 22.0  # Completely untouched
    assert manager.state.total_equity == 36.0
    assert manager.state.realized_loss == 8.0


def test_active_shortfall_handling_and_trade_halt(manager):
    """
    If a loss exceeds active capital:
    - Active capital is floored at $0.00.
    - Protected capital is strictly preserved.
    - Active shortfall is recorded.
    - get_trade_budgets() returns (0, 0) (trading halted).
    """
    manager.process_settlement("reach-44", 33.0, trading_day="2026-11-01")
    # Active = $22.00, Protected = $22.00

    # Suffer catastrophic loss of $25.00 (exceeds $22.00 active pool by $3.00)
    manager.process_settlement("catastrophic-loss", -25.0, trading_day="2026-11-01")

    assert manager.state.active_capital == 0.0
    assert manager.state.active_capital_cents == 0
    assert manager.state.protected_capital == 22.0  # NOT touched or borrowed!
    assert manager.state.total_equity == 22.0
    assert manager.state.active_shortfall_cents == 300
    assert manager.state.active_shortfall == 3.00

    # Budget calculator must halt trading
    init_b, rev_b = manager.get_trade_budgets()
    assert init_b == 0
    assert rev_b == 0


def test_shortfall_recovery_before_compounding(manager):
    """Subsequent profit must absorb active shortfall deficit before resuming 50/50 compounding."""
    manager.process_settlement("reach-44", 33.0, trading_day="2026-11-01")
    # Cause $3.00 shortfall (loss of $25 with $22 active)
    manager.process_settlement("loss-overshoot", -25.0, trading_day="2026-11-01")
    assert manager.state.active_shortfall == 3.00
    assert manager.state.active_capital == 0.0
    assert manager.state.protected_capital == 22.0

    # Now earn $5.00 profit:
    # $3.00 recovers the shortfall (shortfall -> $0.00)
    # Remaining $2.00 splits 50/50: +$1.00 Active, +$1.00 Protected
    manager.process_settlement("recovery-profit", 5.0, trading_day="2026-11-01")

    assert manager.state.active_shortfall_cents == 0
    assert manager.state.active_shortfall == 0.0
    assert manager.state.active_capital == 1.00
    assert manager.state.protected_capital == 23.00
    assert manager.state.total_equity == 24.00

    # Trading is unhalted and derives new whole-dollar budgets
    init_b, rev_b = manager.get_trade_budgets()
    assert init_b == 0  # round(1/11) = 0
    assert rev_b == 1   # round(10/11) = 1


# --- 4. Settlement Idempotency & Late Settlements ---

def test_settlement_idempotency_duplicate_calls(manager):
    """Duplicate calls with the same settlement_id must be completely ignored."""
    manager.process_settlement("settle-idemp-1", 5.0, fee_usd=0.02, trading_day="2026-11-01")
    state1 = manager.state.to_dict()

    # Second identical call
    manager.process_settlement("settle-idemp-1", 5.0, fee_usd=0.02, trading_day="2026-11-01")
    state2 = manager.state.to_dict()

    assert state1["active_capital"] == state2["active_capital"]
    assert state1["total_equity"] == state2["total_equity"]
    assert state1["realized_profit"] == state2["realized_profit"]

    # Verify only 1 transaction exists for this settlement ID
    txs = [t for t in manager.ledger.get_recent_transactions() if t.get("settlement_id") == "settle-idemp-1"]
    assert len(txs) == 1


def test_late_settlement_handling(manager):
    """Settlement from a previous trading day is safely processed and attributed to that day."""
    manager.process_settlement("settle-yesterday", 4.0, fee_usd=0.01, trading_day="2026-10-31")

    txs = manager.ledger.get_recent_transactions()
    yesterday_tx = next((t for t in txs if t["settlement_id"] == "settle-yesterday"), None)
    assert yesterday_tx is not None
    assert yesterday_tx["trading_day"] == "2026-10-31"
    assert yesterday_tx["amount_cents"] == 400


def test_fee_adjusted_net_pnl(manager):
    """Fee tracking records exact fee cents alongside net P&L."""
    manager.process_settlement("settle-with-fee", 2.50, fee_usd=0.15, trading_day="2026-11-01")

    txs = manager.ledger.get_recent_transactions()
    tx = next(t for t in txs if t["settlement_id"] == "settle-with-fee")
    assert tx["fee_cents"] == 15
    assert tx["amount_cents"] == 250


# --- 5. Persistence, Crash Resilience, and Reconciliation ---

def test_crash_resilience_and_reloading(tmp_ledger):
    """State reloaded from SQLite matches exact in-memory state before process shutdown."""
    mgr1 = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    mgr1.process_settlement("reach-44", 33.0, trading_day="2026-11-01")
    mgr1.process_settlement("profit-1", 6.0, trading_day="2026-11-01")

    # Simulate fresh startup / crash recovery
    mgr2 = CapitalManager(ledger=tmp_ledger)
    assert mgr2.state.total_equity == 50.0
    assert mgr2.state.active_capital == 25.0
    assert mgr2.state.protected_capital == 25.0
    assert mgr2.state.milestone_44_activated is True
    assert mgr2.state.last_processed_settlement_id == "profit-1"


def test_reconciliation_with_external(tmp_ledger):
    """Reconciliation correctly detects matching and mismatched external balances."""
    mgr = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    mgr.process_settlement("reach-44", 33.0)

    # Internal total equity = $44.00 (4400 cents)
    in_sync, diff = tmp_ledger.reconcile_with_external(4400)
    assert in_sync is True
    assert diff == 0

    # External reports $44.50 (4450 cents) -> diff = +50 cents
    in_sync, diff = tmp_ledger.reconcile_with_external(4450)
    assert in_sync is False
    assert diff == 50
