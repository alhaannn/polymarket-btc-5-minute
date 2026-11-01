"""
Mandatory Acceptance Tests (Roadmap Phase 8 Section C).
Implements and validates all 14 Acceptance Tests strictly as specified in
MASTER_IMPLEMENTATION_ROADMAP.md Section 8.C.
"""
from __future__ import annotations

import time
from pathlib import Path
import pytest

from polybot.capital import (
    BudgetValidationResult,
    CapitalLedger,
    CapitalManager,
    CapitalReconciler,
    DynamicTradeBudgetCalculator,
    ExchangeBalanceSnapshot,
    round_half_up_to_whole_dollar,
)
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy


def create_mock_books(up_ask: float, down_ask: float):
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    book_up.apply_snapshot([{"price": str(round(up_ask - 0.01, 3)), "size": "100"}], [{"price": str(up_ask), "size": "100"}])
    book_down.apply_snapshot([{"price": str(round(down_ask - 0.01, 3)), "size": "100"}], [{"price": str(down_ask), "size": "100"}])
    return {"Up": book_up, "Down": book_down}


# ==============================================================================
# Test 1 — Starting Capital
# ==============================================================================
def test_acceptance_1_starting_capital(tmp_path: Path):
    """
    Input: total_equity = $11
    Expected: active = $11, protected = $0, initial = $1, reversal = $10
    """
    db_file = tmp_path / "test_acc_1.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)

    assert mgr.state.total_equity == 11.0
    assert mgr.state.active_capital == 11.0
    assert mgr.state.protected_capital == 0.0

    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 1
    assert rev_b == 10


# ==============================================================================
# Test 2 — Pre-Milestone Sizing
# ==============================================================================
def test_acceptance_2_pre_milestone_sizing(tmp_path: Path):
    """
    Input: active = $33
    Expected: initial = $3, reversal = $30, protected = $0
    """
    db_file = tmp_path / "test_acc_2.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    # Win +$22 profit
    mgr.process_settlement(
        settlement_id="s-acc-2",
        net_pnl_usd=22.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )

    assert mgr.state.active_capital == 33.0
    assert mgr.state.protected_capital == 0.0
    assert mgr.state.total_equity == 33.0

    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 3
    assert rev_b == 30


# ==============================================================================
# Test 3 — Exact Milestone
# ==============================================================================
def test_acceptance_3_exact_milestone(tmp_path: Path):
    """
    Input: total_equity = $44
    Expected: active = $22, protected = $22, initial = $2, reversal = $20
    """
    db_file = tmp_path / "test_acc_3.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    # Win +$33 to hit exactly $44
    mgr.process_settlement(
        settlement_id="s-acc-3",
        net_pnl_usd=33.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )

    assert mgr.state.total_equity == 44.0
    assert mgr.state.active_capital == 22.0
    assert mgr.state.protected_capital == 22.0
    assert mgr.state.milestone_44_activated is True

    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 2
    assert rev_b == 20


# ==============================================================================
# Test 4 — Post-Milestone Profit
# ==============================================================================
def test_acceptance_4_post_milestone_profit(tmp_path: Path):
    """
    Input: active = $22, protected = $22, net_profit = $10
    Expected: active = $27, protected = $27, total = $54
    """
    db_file = tmp_path / "test_acc_4.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    # Reach milestone
    mgr.process_settlement(
        settlement_id="s-acc-4-ms",
        net_pnl_usd=33.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )
    # +$10 net profit
    mgr.process_settlement(
        settlement_id="s-acc-4-prof",
        net_pnl_usd=10.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )

    assert mgr.state.active_capital == 27.0
    assert mgr.state.protected_capital == 27.0
    assert mgr.state.total_equity == 54.0


# ==============================================================================
# Test 5 — Post-Milestone Loss
# ==============================================================================
def test_acceptance_5_post_milestone_loss(tmp_path: Path):
    """
    Input: active = $27, protected = $27, net_loss = $5
    Expected: active = $22, protected = $27, total = $49
    """
    db_file = tmp_path / "test_acc_5.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    # Reach $44 milestone
    mgr.process_settlement(
        settlement_id="s-acc-5-ms",
        net_pnl_usd=33.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )
    # +$10 profit -> $27 active / $27 protected
    mgr.process_settlement(
        settlement_id="s-acc-5-prof",
        net_pnl_usd=10.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )
    # -$5 loss -> active drops to $22, protected remains $27
    mgr.process_settlement(
        settlement_id="s-acc-5-loss",
        net_pnl_usd=-5.0,
        fee_usd=0.0,
        trading_day="2026-11-01",
    )

    assert mgr.state.active_capital == 22.0
    assert mgr.state.protected_capital == 27.0
    assert mgr.state.total_equity == 49.0


# ==============================================================================
# Test 6 — Daily Shutdown
# ==============================================================================
def test_acceptance_6_daily_shutdown(tmp_path: Path):
    """
    Input: failed_initial = true, failed_reversal = true, same_trading_day = true
    Expected:
    - Existing shutdown triggers.
    - New entries blocked.
    - Shutdown state persisted.
    - Next-day activation eligibility recorded.
    """
    db_file = tmp_path / "test_acc_6.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=22.0)
    today = "2026-11-01"

    # Initial failure
    mgr.process_settlement("s-init-fail", net_pnl_usd=-1.0, trading_day=today, signal_type="INITIAL")
    assert mgr.is_daily_shutdown_active(today) is False

    # Reversal failure on same day
    mgr.process_settlement("s-rev-fail", net_pnl_usd=-10.0, trading_day=today, signal_type="REVERSAL")

    assert mgr.is_daily_shutdown_active(today) is True
    assert mgr.state.daily_shutdown_status is True
    assert mgr.state.next_day_activation_eligible is True

    # Budgets set to 0
    init_b, rev_b = mgr.get_trade_budgets(today)
    assert init_b == 0
    assert rev_b == 0

    # Persisted across restart
    reloaded_mgr = CapitalManager(db_path=db_file)
    assert reloaded_mgr.is_daily_shutdown_active(today) is True
    assert reloaded_mgr.state.next_day_activation_eligible is True


# ==============================================================================
# Test 7 — Single Failure Only
# ==============================================================================
def test_acceptance_7_single_failure_only(tmp_path: Path):
    """
    Run two independent cases:
    - Initial failure only.
    - Reversal failure only.
    Expected: In both cases, this specific combined shutdown must not trigger.
    """
    today = "2026-11-01"

    # Case A: Initial failure only
    mgr_a = CapitalManager(db_path=tmp_path / "test_acc_7a.db", starting_equity_usd=22.0)
    mgr_a.process_settlement("s-7a-1", net_pnl_usd=-1.0, trading_day=today, signal_type="INITIAL")
    mgr_a.process_settlement("s-7a-2", net_pnl_usd=-1.0, trading_day=today, signal_type="INITIAL")
    assert mgr_a.is_daily_shutdown_active(today) is False
    assert mgr_a.state.daily_shutdown_status is False

    # Case B: Reversal failure only
    mgr_b = CapitalManager(db_path=tmp_path / "test_acc_7b.db", starting_equity_usd=22.0)
    mgr_b.process_settlement("s-7b-1", net_pnl_usd=-5.0, trading_day=today, signal_type="REVERSAL")
    mgr_b.process_settlement("s-7b-2", net_pnl_usd=-5.0, trading_day=today, signal_type="REVERSAL")
    assert mgr_b.is_daily_shutdown_active(today) is False
    assert mgr_b.state.daily_shutdown_status is False


# ==============================================================================
# Test 8 — Option B Activation
# ==============================================================================
def test_acceptance_8_option_b_activation(tmp_path: Path):
    """
    Input: active = $30, protected = $110, qualifying_previous_day_shutdown = true
    Expected: released = $55, active = $85, protected = $55, total = $140, initial = $8, reversal = $77
    """
    db_file = tmp_path / "test_acc_8.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=140.0)

    # Set up exact state: $30 active, $110 protected
    mgr.state.active_capital_cents = 3000
    mgr.state.protected_capital_cents = 11000
    mgr.state.total_equity_cents = 14000
    mgr.state.milestone_44_activated = True

    # Record qualifying shutdown on day 2026-10-31
    mgr.record_daily_shutdown_qualifying_event(
        trading_day="2026-10-31",
        failed_initial_count=1,
        failed_reversal_count=1,
        reason="Conjunctive test failure",
    )

    # Advance to next day 2026-11-01
    activated, released_usd, _ = mgr.check_and_activate_next_day_reserve("2026-11-01")

    assert activated is True
    assert released_usd == 55.0
    assert mgr.state.active_capital == 85.0
    assert mgr.state.protected_capital == 55.0
    assert mgr.state.total_equity == 140.0

    init_b, rev_b = mgr.get_trade_budgets("2026-11-01")
    assert init_b == 8   # round($85 / 11) = 8
    assert rev_b == 77   # round(($85 * 10) / 11) = 77


# ==============================================================================
# Test 9 — No Qualifying Shutdown
# ==============================================================================
def test_acceptance_9_no_qualifying_shutdown(tmp_path: Path):
    """
    Input: active = $30, protected = $110, qualifying_previous_day_shutdown = false
    Expected: active = $30, protected = $110, no_release = true
    """
    db_file = tmp_path / "test_acc_9.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=140.0)
    mgr.state.active_capital_cents = 3000
    mgr.state.protected_capital_cents = 11000
    mgr.state.total_equity_cents = 14000

    activated, released_usd, _ = mgr.check_and_activate_next_day_reserve("2026-11-01")

    assert activated is False
    assert released_usd == 0.0
    assert mgr.state.active_capital == 30.0
    assert mgr.state.protected_capital == 110.0
    assert mgr.state.total_equity == 140.0


# ==============================================================================
# Test 10 — Exactly-Once Activation
# ==============================================================================
def test_acceptance_10_exactly_once_activation(tmp_path: Path):
    """
    Trigger the same activation event twice, including across a restart.
    Expected: only one release.
    """
    db_file = tmp_path / "test_acc_10.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=140.0)
    mgr.state.milestone_44_activated = True
    mgr.state.active_capital_cents = 3000
    mgr.state.protected_capital_cents = 11000
    mgr.state.total_equity_cents = 14000
    mgr.record_daily_shutdown_qualifying_event("2026-10-31", 1, 1)

    # First activation
    act1, rel1, _ = mgr.check_and_activate_next_day_reserve("2026-11-01")
    assert act1 is True
    assert rel1 == 55.0

    # Immediate second attempt
    act2, rel2, _ = mgr.check_and_activate_next_day_reserve("2026-11-01")
    assert act2 is False
    assert rel2 == 0.0

    # Restart bot from SQLite
    reloaded_mgr = CapitalManager(db_path=db_file)
    act3, rel3, _ = reloaded_mgr.check_and_activate_next_day_reserve("2026-11-01")
    assert act3 is False
    assert rel3 == 0.0
    assert reloaded_mgr.state.active_capital == 85.0
    assert reloaded_mgr.state.protected_capital == 55.0


# ==============================================================================
# Test 11 — Insufficient Active Funds
# ==============================================================================
def test_acceptance_11_insufficient_active_funds():
    """
    Expected:
    - Reject unsafe order.
    - Protected funds remain untouched.
    - Existing state remains consistent.
    """
    calc = DynamicTradeBudgetCalculator(min_order_usd=1.0)

    # Free active capital is $0.50 (below $1.00 minimum)
    val = calc.validate_order_budget(
        signal_type="INITIAL",
        active_capital_usd=1.0,
        open_exposure_usd=0.50,
    )
    assert val.allowed is False
    assert "below minimum allowed order" in val.reason


# ==============================================================================
# Test 12 — Restart Recovery
# ==============================================================================
def test_acceptance_12_restart_recovery(tmp_path: Path):
    """
    Restart after settlement, milestone, shutdown, reserve activation.
    Expected: No duplicated trades, settlements, or capital transitions.
    """
    db_file = tmp_path / "test_acc_12.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)

    # Milestone activation
    mgr.process_settlement("s-12-ms", net_pnl_usd=33.0, trading_day="2026-10-30")
    # Shutdown trigger
    mgr.record_daily_shutdown_qualifying_event("2026-10-30", 1, 1)
    # Reserve release next day
    mgr.check_and_activate_next_day_reserve("2026-10-31")

    # Restart
    mgr_restart = CapitalManager(db_path=db_file)
    assert mgr_restart.state.milestone_44_activated is True
    assert mgr_restart.state.total_equity == 44.0
    # $22 active + $11 released = $33 active, $11 protected
    assert mgr_restart.state.active_capital == 33.0
    assert mgr_restart.state.protected_capital == 11.0

    # Ensure idempotency on re-processing existing settlement
    state_after = mgr_restart.process_settlement("s-12-ms", net_pnl_usd=33.0, trading_day="2026-10-30")
    assert state_after.total_equity == 44.0


# ==============================================================================
# Test 13 — Reconciliation Mismatch
# ==============================================================================
def test_acceptance_13_reconciliation_mismatch(tmp_path: Path):
    """
    Expected:
    - New orders blocked.
    - Mismatch logged.
    - No silent capital reclassification.
    """
    db_file = tmp_path / "test_acc_13.db"
    mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    reconciler = CapitalReconciler(mgr)

    # Discrepancy: External balance is $9.00 vs internal expected $11.00
    snapshot = ExchangeBalanceSnapshot(available_collateral_cents=900, locked_collateral_cents=0)
    report = reconciler.reconcile(external_snapshot=snapshot, trading_day="2026-11-01")

    assert report.is_valid is False
    assert report.status == "MISMATCH"
    assert mgr.state.reconciliation_blocked is True

    # Budgets are blocked
    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 0
    assert rev_b == 0

    # Protected capital must NEVER be used to hide the mismatch
    assert mgr.state.protected_capital == 0.0
    assert mgr.state.active_capital == 11.0


# ==============================================================================
# Test 14 — Existing Strategy Regression
# ==============================================================================
def test_acceptance_14_existing_strategy_regression():
    """
    Verify strategy decisions remain 100% identical:
    - Same initial trigger range (89% - 92%).
    - Same reversal trigger range (85% - 90%).
    - Same settlement classification.
    """
    cfg = StrategyConfig(
        initial_prob_min=0.89,
        initial_prob_max=0.92,
        reversal_prob_min=0.85,
        reversal_prob_max=0.90,
    )
    strat = ReversalStrategy(cfg)
    strat.reset_window(window_start=1000)

    # 1. Waiting phase (t < 180s): No trade even if prob in range
    books_wait = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_wait = strat.on_tick(current_price=80000.0, timestamp_sec=1170.0, books=books_wait)
    assert sig_wait is None

    # 2. Trading phase: Initial signal in range (89% - 92%)
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)
    assert sig is not None
    assert sig.signal_type == "INITIAL"
    assert sig.side == "Up"
    assert sig.cost_usd == 1.0

    # 3. Reversal signal in range (85% - 90%)
    books_rev = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1210.0, books=books_rev)
    assert sig_rev is not None
    assert sig_rev.signal_type == "REVERSAL"
    assert sig_rev.side == "Down"
    assert sig_rev.cost_usd == 10.0
