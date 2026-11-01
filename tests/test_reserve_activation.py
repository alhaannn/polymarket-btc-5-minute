"""
Unit and integration tests for Roadmap Phase 6: Next-Day Protected Capital Activation (Option B).
Verifies:
1. Canonical $30 Active / $110 Protected -> $85 Active / $55 Protected release example.
2. Canonical sizing derivation ($8 Initial / $77 Reversal).
3. Repeated qualifying shutdown (second release adds 50% of remaining protected capital).
4. No qualifying shutdown -> no release.
5. Zero protected capital -> no release.
6. Same-day activation guard (cannot activate on same day as shutdown).
7. Exactly-once activation idempotency (duplicate call returns False).
8. Crash and restart on next day (cold-start recovery performs release once).
9. Crash after commit does not double release.
10. Late settlement after reserve activation.
11. Open position preservation at activation time (free active calculation incorporates release).
12. Odd-cent protected capital release (penny conservation).
"""
from pathlib import Path
import pytest
import time

from polybot.capital.ledger import CapitalLedger
from polybot.capital.manager import CapitalManager
from polybot.capital.models import CapitalState, CapitalTransaction, TransactionType
from polybot.capital.sizing import DynamicTradeBudgetCalculator
from polybot.config import StrategyConfig
from polybot.execution.base import Order
from polybot.execution.paper import PaperExecutionEngine


@pytest.fixture
def tmp_ledger(tmp_path: Path):
    db_path = tmp_path / "test_reserve.db"
    return CapitalLedger(db_path)


def setup_manager_with_balances(
    ledger: CapitalLedger,
    active_usd: float,
    protected_usd: float,
    trading_day: str = "2026-11-01",
) -> CapitalManager:
    """Helper to initialize a CapitalManager with specific Active and Protected balances."""
    active_cents = int(round(active_usd * 100))
    prot_cents = int(round(protected_usd * 100))
    tot_cents = active_cents + prot_cents

    state = CapitalState(
        total_equity_cents=tot_cents,
        active_capital_cents=active_cents,
        protected_capital_cents=prot_cents,
        milestone_44_activated=(prot_cents > 0 or tot_cents >= 4400),
        daily_failure_trading_day=trading_day,
        updated_at_ts=time.time(),
    )
    tx = CapitalTransaction(
        tx_id="tx-setup",
        event_type=TransactionType.INITIAL_CAPITAL,
        amount_cents=tot_cents,
        active_before_cents=0,
        active_after_cents=active_cents,
        protected_before_cents=0,
        protected_after_cents=prot_cents,
        total_before_cents=0,
        total_after_cents=tot_cents,
        trading_day=trading_day,
        reason="Test setup balances",
        idempotency_key="setup-key",
    )
    ledger.save_state_and_transaction(state, tx)
    return CapitalManager(ledger=ledger)


# --- 1. Canonical Option B Activation Tests ---

def test_canonical_example_release_and_sizing(tmp_ledger):
    """
    Roadmap Section 1.6 & Phase 6 Canonical Example:
    Before: Active = $30.00, Protected = $110.00, Total = $140.00.
    Day 1 shutdown fires.
    Day 2 activates 50% of Protected ($55.00).
    After: Active = $85.00, Protected = $55.00, Total = $140.00.
    New Sizing: Initial = $8, Reversal = $77.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    assert mgr.state.active_capital == 30.0
    assert mgr.state.protected_capital == 110.0
    assert mgr.state.total_equity == 140.0

    # Day 1: Trigger conjunctive daily shutdown
    mgr.process_settlement("init-fail", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-fail", -5.0, trading_day=day1, signal_type="REVERSAL")
    assert mgr.is_daily_shutdown_active(day1) is True
    # Active reduced by $6 of losses ($24.00 active, $110.00 protected)
    assert mgr.state.active_capital == 24.0
    assert mgr.state.protected_capital == 110.0

    # Set back to $30 active for exact test of canonical example
    mgr.state.active_capital_cents = 3000
    mgr.state.total_equity_cents = 14000
    mgr.verify_invariants()

    # Day 2: Next day begins. Activate Option B reserve release!
    activated, released_usd, msg = mgr.check_and_activate_next_day_reserve(day2)

    assert activated is True
    assert released_usd == 55.00
    assert "Released $55.00" in msg

    # Balances after release
    assert mgr.state.active_capital == 85.00
    assert mgr.state.protected_capital == 55.00
    assert mgr.state.total_equity == 140.00  # Total equity unchanged!
    assert mgr.state.active_capital_cents == 8500
    assert mgr.state.protected_capital_cents == 5500
    assert mgr.state.total_equity_cents == 14000

    # Sizing verification: round(85/11) = $8, round((85*10)/11) = $77
    init_b, rev_b = mgr.get_trade_budgets(day2)
    assert init_b == 8
    assert rev_b == 77


def test_repeated_qualifying_shutdown_later(tmp_ledger):
    """
    Roadmap Section 1.6 & Phase 6 Repeated Shutdown Example:
    Protected = $55.00.
    Later shutdown occurs on Day 3.
    Day 4 releases 50% of $55.00 = $27.50.
    Remaining Protected = $27.50.
    Added to existing active capital!
    """
    day3 = "2026-11-03"
    day4 = "2026-11-04"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=85.0, protected_usd=55.0, trading_day=day3)

    # Day 3: Another qualifying shutdown
    mgr.process_settlement("init-fail-d3", -1.0, trading_day=day3, signal_type="INITIAL")
    mgr.process_settlement("rev-fail-d3", -5.0, trading_day=day3, signal_type="REVERSAL")
    assert mgr.is_daily_shutdown_active(day3) is True

    # Active was $85 - $6 = $79. Protected is $55.
    assert mgr.state.active_capital == 79.0
    assert mgr.state.protected_capital == 55.0

    # Day 4: Activate second release
    activated, released_usd, msg = mgr.check_and_activate_next_day_reserve(day4)

    assert activated is True
    assert released_usd == 27.50
    # $79.00 existing active + $27.50 released = $106.50
    assert mgr.state.active_capital == 106.50
    assert mgr.state.protected_capital == 27.50
    assert mgr.state.total_equity == 134.00  # $79 + $55 = $134. Unchanged!


# --- 2. Guard & Eligibility Tests ---

def test_no_shutdown_means_no_release(tmp_ledger):
    """Without a qualifying shutdown event, reserve activation must not occur."""
    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day="2026-11-01")

    activated, released, msg = mgr.check_and_activate_next_day_reserve("2026-11-02")
    assert activated is False
    assert released == 0.0
    assert "No qualifying shutdown event" in msg
    assert mgr.state.active_capital == 30.0
    assert mgr.state.protected_capital == 110.0


def test_no_protected_capital_means_no_release(tmp_ledger):
    """If protected capital is $0, reserve activation must safely exit without release."""
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=20.0, protected_usd=0.0, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")
    assert mgr.is_daily_shutdown_active(day1) is True

    activated, released, msg = mgr.check_and_activate_next_day_reserve(day2)
    assert activated is False
    assert released == 0.0
    assert "Protected capital is zero" in msg
    assert mgr.state.active_capital == 14.0
    assert mgr.state.protected_capital == 0.0


def test_cannot_activate_on_same_day_as_shutdown(tmp_ledger):
    """Reserve activation cannot execute on the same day the shutdown occurred."""
    day1 = "2026-11-01"
    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")

    activated, released, msg = mgr.check_and_activate_next_day_reserve(day1)
    assert activated is False
    assert "Cannot activate reserve on or before shutdown day" in msg


def test_repeated_activation_attempt_exact_once(tmp_ledger):
    """A qualifying event can be activated at most once; repeated calls return False."""
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")

    # First activation: succeeds
    act1, rel1, _ = mgr.check_and_activate_next_day_reserve(day2)
    assert act1 is True
    assert rel1 == 55.0

    # Second immediate activation attempt: must be rejected
    act2, rel2, msg2 = mgr.check_and_activate_next_day_reserve(day2)
    assert act2 is False
    assert rel2 == 0.0
    assert "No qualifying shutdown event" in msg2


# --- 3. Crash Resilience & Persistence Tests ---

def test_restart_next_day_performs_activation_once(tmp_ledger):
    """
    If the bot is turned off on Day 1 after shutdown and restarted on Day 2:
    The new process instance performs Option B activation cleanly on first run.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr1 = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr1.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr1.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")
    # Simulate process shutdown

    # Start fresh process on Day 2
    mgr2 = CapitalManager(ledger=tmp_ledger)
    activated, released_usd, _ = mgr2.check_and_activate_next_day_reserve(day2)
    assert activated is True
    assert released_usd == 55.0
    assert mgr2.state.active_capital == 79.0  # ($30 - $6 + $55)
    assert mgr2.state.protected_capital == 55.0


def test_crash_after_commit_does_not_double_release(tmp_ledger):
    """
    If the bot activates on Day 2, commits to SQLite, and then crashes:
    Reloading on Day 2 does NOT double-release.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr1 = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr1.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr1.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")
    mgr1.check_and_activate_next_day_reserve(day2)
    # Committed: Active=$79, Protected=$55

    # Crash and reload
    mgr2 = CapitalManager(ledger=tmp_ledger)
    act2, rel2, _ = mgr2.check_and_activate_next_day_reserve(day2)
    assert act2 is False
    assert rel2 == 0.0
    assert mgr2.state.active_capital == 79.0
    assert mgr2.state.protected_capital == 55.0


# --- 4. Edge Cases: Odd-Cents, Open Positions, and Late Settlements ---

def test_odd_cent_protected_capital_release(tmp_ledger):
    """
    If protected capital has an odd cent ($55.01 = 5501 cents):
    Released = 5501 // 2 = 2750 cents ($27.50).
    Remaining Protected = 2751 cents ($27.51).
    Total equity is preserved to the exact penny.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=55.01, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")

    activated, released_usd, _ = mgr.check_and_activate_next_day_reserve(day2)
    assert activated is True
    assert released_usd == 27.50
    assert mgr.state.protected_capital_cents == 2751
    assert mgr.state.protected_capital == 27.51
    # $24.00 active + $27.50 = $51.50 (5150 cents)
    assert mgr.state.active_capital_cents == 5150
    assert mgr.state.total_equity_cents == (5150 + 2751)


def test_open_position_at_activation_time(tmp_ledger):
    """
    If an open position exists across the midnight boundary:
    Reserve release increases active capital, and free active funds calculation
    correctly incorporates the released funds minus open exposure.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")

    # Sizer calculator
    calc = DynamicTradeBudgetCalculator(min_order_usd=1.0)
    open_exposure = 10.0  # $10 open position

    # Before activation: Active is $24 -> free active = $14
    res_before = calc.validate_order_budget("INITIAL", mgr.state.active_capital, open_exposure_usd=open_exposure)
    assert res_before.allowed is True
    assert res_before.free_active_usd == 14.0

    # Activate Option B release on Day 2
    mgr.check_and_activate_next_day_reserve(day2)
    assert mgr.state.active_capital == 79.0  # $24 + $55

    # After activation: Active is $79 -> free active = $69
    res_after = calc.validate_order_budget("INITIAL", mgr.state.active_capital, open_exposure_usd=open_exposure)
    assert res_after.allowed is True
    assert res_after.free_active_usd == 69.0
    assert res_after.approved_budget_usd == 7.0  # round(79/11) = 7


def test_late_settlement_after_reserve_activation(tmp_ledger):
    """Late settlement from Day 1 arriving on Day 2 after activation processes cleanly."""
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = setup_manager_with_balances(tmp_ledger, active_usd=30.0, protected_usd=110.0, trading_day=day1)
    mgr.process_settlement("init-f", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-f", -5.0, trading_day=day1, signal_type="REVERSAL")

    # Activate on Day 2 (Active -> $79, Protected -> $55)
    mgr.check_and_activate_next_day_reserve(day2)

    # Late settlement from Day 1 arrives with +$10 profit
    mgr.process_settlement("late-d1-win", 10.0, fee_usd=0.05, trading_day=day1, signal_type="INITIAL")

    # Post-milestone profit of $10 splits 50/50: +$5 Active, +$5 Protected
    assert mgr.state.active_capital == 84.0  # $79 + $5
    assert mgr.state.protected_capital == 60.0  # $55 + $5
    assert mgr.state.total_equity == 144.0
