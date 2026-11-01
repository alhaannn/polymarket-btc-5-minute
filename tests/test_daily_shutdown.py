"""
Unit and integration tests for Roadmap Phase 5: Integrate Existing Daily Shutdown.
Verifies the exact conjunctive daily shutdown rule:
  failed_initial_count >= 1 AND failed_reversal_count >= 1 on the same configured trading day.
Verifies persistence, crash restart resilience across all failure stages, day boundary rollover,
safe handling of open positions, and pending order cancellation.
"""
from pathlib import Path
import pytest
import time

from polybot.capital.ledger import CapitalLedger
from polybot.capital.manager import CapitalManager
from polybot.config import StrategyConfig
from polybot.execution.base import Order
from polybot.execution.paper import PaperExecutionEngine


@pytest.fixture
def tmp_ledger(tmp_path: Path):
    db_path = tmp_path / "test_shutdown.db"
    return CapitalLedger(db_path)


@pytest.fixture
def manager(tmp_ledger):
    return CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)


# --- 1. Conjunctive Trigger Verification ---

def test_initial_failure_alone_does_not_trigger_shutdown(manager):
    """One failed initial trade alone must NOT trigger daily shutdown."""
    day = "2026-11-01"
    manager.process_settlement(
        settlement_id="init-fail-1",
        net_pnl_usd=-1.0,
        fee_usd=0.01,
        trading_day=day,
        signal_type="INITIAL",
    )

    assert manager.state.daily_failed_initial_count == 1
    assert manager.state.daily_failed_reversal_count == 0
    assert manager.state.daily_shutdown_status is False
    assert manager.is_daily_shutdown_active(day) is False

    # Budget should still be allowed
    init_b, rev_b = manager.get_trade_budgets(day)
    assert init_b > 0
    assert rev_b > 0


def test_multiple_initial_failures_alone_do_not_trigger_shutdown(manager):
    """Multiple initial failures without a reversal failure must NOT trigger daily shutdown."""
    day = "2026-11-01"
    for i in range(3):
        manager.process_settlement(
            settlement_id=f"init-fail-{i}",
            net_pnl_usd=-1.0,
            fee_usd=0.01,
            trading_day=day,
            signal_type="INITIAL",
        )

    assert manager.state.daily_failed_initial_count == 3
    assert manager.state.daily_failed_reversal_count == 0
    assert manager.state.daily_shutdown_status is False
    assert manager.is_daily_shutdown_active(day) is False


def test_reversal_failure_alone_does_not_trigger_shutdown(manager):
    """One failed reversal trade alone must NOT trigger daily shutdown."""
    day = "2026-11-01"
    manager.process_settlement(
        settlement_id="rev-fail-1",
        net_pnl_usd=-5.0,
        fee_usd=0.02,
        trading_day=day,
        signal_type="REVERSAL",
    )

    assert manager.state.daily_failed_initial_count == 0
    assert manager.state.daily_failed_reversal_count == 1
    assert manager.state.daily_shutdown_status is False
    assert manager.is_daily_shutdown_active(day) is False


def test_conjunctive_both_failures_on_same_day_triggers_shutdown(manager):
    """
    At least 1 initial trade fails AND at least 1 reversal trade fails on the same trading day:
    Triggers daily shutdown, blocks new trade budgets, and creates qualifying shutdown event.
    """
    day = "2026-11-01"
    # 1. Initial trade fails
    manager.process_settlement(
        settlement_id="settle-init-loss",
        net_pnl_usd=-1.0,
        trading_day=day,
        signal_type="INITIAL",
    )
    assert manager.state.daily_shutdown_status is False

    # 2. Reversal trade fails on same day
    manager.process_settlement(
        settlement_id="settle-rev-loss",
        net_pnl_usd=-5.0,
        trading_day=day,
        signal_type="REVERSAL",
    )

    # Conjunctive condition met!
    assert manager.state.daily_failed_initial_count == 1
    assert manager.state.daily_failed_reversal_count == 1
    assert manager.state.daily_shutdown_status is True
    assert manager.state.daily_shutdown_trading_day == day
    assert manager.state.next_day_activation_eligible is True
    assert "Conjunctive failure triggered" in manager.state.daily_shutdown_reason

    # Sizing returns 0, 0
    init_b, rev_b = manager.get_trade_budgets(day)
    assert init_b == 0
    assert rev_b == 0

    # Exactly 1 unactivated qualifying shutdown event exists
    event = manager.ledger.get_unactivated_shutdown_event()
    assert event is not None
    assert event["trading_day"] == day
    assert event["failed_initial_count"] == 1
    assert event["failed_reversal_count"] == 1
    assert event["activated"] == 0


def test_failures_on_different_days_do_not_trigger_shutdown(tmp_ledger):
    """
    Initial fails on Day 1, Reversal fails on Day 2:
    Since they belong to different trading days, conjunctive rule does NOT fire.
    """
    mgr = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    # Day 1: Initial fails
    mgr.process_settlement("init-day1", -1.0, trading_day=day1, signal_type="INITIAL")
    assert mgr.state.daily_failed_initial_count == 1
    assert mgr.state.daily_shutdown_status is False

    # Day 2: Reversal fails
    mgr.process_settlement("rev-day2", -5.0, trading_day=day2, signal_type="REVERSAL")
    # Day 2 should have 0 initial failures and 1 reversal failure
    assert mgr.state.daily_failed_initial_count == 0
    assert mgr.state.daily_failed_reversal_count == 1
    assert mgr.state.daily_shutdown_status is False
    assert mgr.is_daily_shutdown_active(day2) is False


# --- 2. Restart and Crash Resilience Tests ---

def test_restart_before_first_failure(tmp_ledger):
    """Process restart before any failure preserves clean zero-failure state."""
    mgr1 = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    assert mgr1.state.daily_failed_initial_count == 0
    assert mgr1.state.daily_failed_reversal_count == 0

    mgr2 = CapitalManager(ledger=tmp_ledger)
    assert mgr2.state.daily_failed_initial_count == 0
    assert mgr2.state.daily_failed_reversal_count == 0
    assert mgr2.state.daily_shutdown_status is False


def test_restart_after_initial_failure_only(tmp_ledger):
    """
    After an initial failure only, restarting the bot reloads the exact counter.
    Subsequent reversal failure then triggers the shutdown.
    """
    day = "2026-11-01"
    mgr1 = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    mgr1.process_settlement("init-1", -1.0, trading_day=day, signal_type="INITIAL")
    assert mgr1.state.daily_failed_initial_count == 1
    assert mgr1.state.daily_shutdown_status is False

    # Simulate restart
    mgr2 = CapitalManager(ledger=tmp_ledger)
    assert mgr2.state.daily_failed_initial_count == 1
    assert mgr2.state.daily_failed_reversal_count == 0
    assert mgr2.state.daily_shutdown_status is False

    # Now reversal fails in the new process session
    mgr2.process_settlement("rev-1", -5.0, trading_day=day, signal_type="REVERSAL")
    assert mgr2.state.daily_shutdown_status is True
    assert mgr2.is_daily_shutdown_active(day) is True


def test_restart_after_reversal_failure_only(tmp_ledger):
    """
    After a reversal failure only, restarting preserves the counter.
    Subsequent initial failure triggers the shutdown.
    """
    day = "2026-11-01"
    mgr1 = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    mgr1.process_settlement("rev-1", -5.0, trading_day=day, signal_type="REVERSAL")
    assert mgr1.state.daily_failed_reversal_count == 1

    # Simulate restart
    mgr2 = CapitalManager(ledger=tmp_ledger)
    assert mgr2.state.daily_failed_initial_count == 0
    assert mgr2.state.daily_failed_reversal_count == 1
    assert mgr2.state.daily_shutdown_status is False

    # Initial fails
    mgr2.process_settlement("init-1", -1.0, trading_day=day, signal_type="INITIAL")
    assert mgr2.state.daily_shutdown_status is True


def test_restart_after_shutdown_triggered_cannot_be_bypassed(tmp_ledger):
    """
    After shutdown has triggered, restarting the bot must NOT bypass the shutdown.
    The shutdown status remains active and budgets remain $0.
    """
    day = "2026-11-01"
    mgr1 = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    mgr1.process_settlement("init-1", -1.0, trading_day=day, signal_type="INITIAL")
    mgr1.process_settlement("rev-1", -5.0, trading_day=day, signal_type="REVERSAL")
    assert mgr1.state.daily_shutdown_status is True

    # Simulate restart
    mgr2 = CapitalManager(ledger=tmp_ledger)
    assert mgr2.state.daily_shutdown_status is True
    assert mgr2.state.daily_shutdown_trading_day == day
    assert mgr2.is_daily_shutdown_active(day) is True

    init_b, rev_b = mgr2.get_trade_budgets(day)
    assert init_b == 0
    assert rev_b == 0


def test_daily_boundary_rollover_resets_shutdown_for_new_day(tmp_ledger):
    """
    When the next trading day arrives:
    - Daily shutdown deactivates for the new day.
    - Daily failure counters reset to 0.
    - Qualifying shutdown event from yesterday remains in SQLite for Option B reserve activation.
    """
    day1 = "2026-11-01"
    day2 = "2026-11-02"

    mgr = CapitalManager(ledger=tmp_ledger, starting_equity_usd=22.0)
    mgr.process_settlement("init-1", -1.0, trading_day=day1, signal_type="INITIAL")
    mgr.process_settlement("rev-1", -5.0, trading_day=day1, signal_type="REVERSAL")
    assert mgr.is_daily_shutdown_active(day1) is True

    # Day 2 arrives
    assert mgr.is_daily_shutdown_active(day2) is False
    assert mgr.state.daily_failed_initial_count == 0
    assert mgr.state.daily_failed_reversal_count == 0
    assert mgr.state.daily_failure_trading_day == day2

    # Sizing for Day 2 is unblocked (Active = $16 -> init=$1, rev=$15)
    init_b, rev_b = mgr.get_trade_budgets(day2)
    assert init_b > 0
    assert rev_b > 0

    # The qualifying event from Day 1 is intact and eligible for Option B
    assert mgr.state.next_day_activation_eligible is True
    event = mgr.ledger.get_unactivated_shutdown_event()
    assert event is not None
    assert event["trading_day"] == day1


# --- 3. Open Positions and Order Cancellation Safety ---

def test_unresolved_positions_preserved_through_shutdown(tmp_ledger):
    """
    Already-open positions must not be abandoned or falsely marked settled when shutdown triggers.
    They continue tracking and settle normally when market resolves.
    """
    cfg = StrategyConfig()
    engine = PaperExecutionEngine(cfg)

    # Open a position
    order = Order(
        order_id="order-open-1",
        slug="btc-5m-window-1",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=1.0,
        shares=1.11,
        created_at_ts=time.time(),
    )
    engine.submit_order(order)
    # Simulate immediate fill
    engine._record_fill(
        fill=type("Fill", (), {
            "fill_id": "f1",
            "order_id": "order-open-1",
            "slug": "btc-5m-window-1",
            "side": "Up",
            "action": "BUY",
            "price": 0.90,
            "shares": 1.11,
            "cost_usd": 1.0,
            "fee_usd": 0.001,
            "filled_at_ts": time.time(),
            "signal_type": "INITIAL",
            "linked_order_id": None,
            "trigger_prob": 0.90,
            "counterpart_prob": 0.10,
            "data_receipt_ts": time.time(),
        })(),
        order=order
    )
    assert len(engine.open_positions_list) == 1
    open_pos = engine.open_positions_list[0]
    assert open_pos.status == "OPEN"

    # Now shutdown occurs in capital manager
    mgr = CapitalManager(ledger=tmp_ledger, starting_equity_usd=11.0)
    day = "2026-11-01"
    mgr.process_settlement("init-fail", -1.0, trading_day=day, signal_type="INITIAL")
    mgr.process_settlement("rev-fail", -5.0, trading_day=day, signal_type="REVERSAL")
    assert mgr.is_daily_shutdown_active(day) is True

    # Position is STILL OPEN and NOT abandoned!
    assert len(engine.open_positions_list) == 1
    assert open_pos.status == "OPEN"

    # Later, position resolves with a win
    settled = engine.settle_window_all("btc-5m-window-1", winner="Up", settle_ts=time.time())
    assert len(settled) == 1
    assert settled[0].status == "SETTLED"
    assert settled[0].realized_pnl_usd > 0


def test_safe_cancellation_of_unfilled_pending_orders():
    """Unfilled pending orders are safely cancelled on shutdown without disturbing open positions."""
    cfg = StrategyConfig()
    engine = PaperExecutionEngine(cfg)

    # Submit 2 pending orders waiting for latency
    now = time.time()
    o1 = Order(order_id="p1", slug="s1", side="Up", action="BUY", order_type="taker", price=0.90, usd_amount=1.0, shares=1.1, created_at_ts=now)
    o2 = Order(order_id="p2", slug="s2", side="Down", action="BUY", order_type="taker", price=0.88, usd_amount=10.0, shares=11.3, created_at_ts=now)
    engine.submit_order(o1)
    engine.submit_order(o2)
    assert len(engine.pending_orders) == 2

    # Call cancellation on shutdown
    cancelled = engine.cancel_pending_orders()
    assert cancelled == 2
    assert len(engine.pending_orders) == 0
    assert o1.status == "CANCELLED"
    assert o2.status == "CANCELLED"
