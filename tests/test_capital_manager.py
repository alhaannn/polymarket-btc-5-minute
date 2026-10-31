"""
Unit tests for Dynamic Capital Management & Compounding Engine (Roadmap Phase 2).
Validates canonical sizing, $44 milestone, 50/50 profit compounding, active-only loss deduction,
Option B next-day reserve release, idempotency, and persistence.
"""
from pathlib import Path
import pytest

from polybot.capital.manager import CapitalManager, round_half_up_to_whole_dollar
from polybot.capital.ledger import CapitalLedger
from polybot.capital.models import TransactionType


def test_starting_capital_initialization(tmp_path: Path):
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=11.0)

    assert mgr.state.total_equity == 11.0
    assert mgr.state.active_capital == 11.0
    assert mgr.state.protected_capital == 0.0
    assert not mgr.state.milestone_44_activated

    # Initial sizing at $11 -> $1 / $10
    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 1
    assert rev_b == 10


def test_canonical_budget_rounding_table(tmp_path: Path):
    """Verifies all exact balances and budgets from Roadmap Section 1.2 Table."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=11.0)

    table = [
        (11.0, 1, 10),
        (22.0, 2, 20),
        (33.0, 3, 30),
        (40.0, 4, 36),
        (43.0, 4, 39),
        (44.0, 4, 40),
        (55.0, 5, 50),
        (66.0, 6, 60),
        (88.0, 8, 80),
        (110.0, 10, 100),
        (220.0, 20, 200),
    ]

    for active_val, expected_init, expected_rev in table:
        mgr.state.active_capital_cents = int(active_val * 100)
        mgr.state.total_equity_cents = mgr.state.active_capital_cents + mgr.state.protected_capital_cents
        init_b, rev_b = mgr.get_trade_budgets()
        assert init_b == expected_init, f"Failed init budget for active={active_val}: expected {expected_init}, got {init_b}"
        assert rev_b == expected_rev, f"Failed rev budget for active={active_val}: expected {expected_rev}, got {rev_b}"


def test_first_protection_milestone_at_44(tmp_path: Path):
    """Verifies that crossing $44 milestone triggers exactly once and splits equity 50/50."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=11.0)

    # 1. Trade profit of $33 brings total equity to exactly $44.00
    mgr.process_settlement(settlement_id="set-1", net_pnl_usd=33.0)

    assert mgr.state.milestone_44_activated
    assert mgr.state.total_equity == 44.0
    assert mgr.state.active_capital == 22.0
    assert mgr.state.protected_capital == 22.0

    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 2
    assert rev_b == 20


def test_post_milestone_profit_allocation(tmp_path: Path):
    """Section 1.4: After milestone, subsequent profits split 50% Active / 50% Protected."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=44.0)
    # Manually trigger milestone
    mgr.process_settlement(settlement_id="set-init", net_pnl_usd=0.0)
    mgr.state.total_equity_cents = 4400
    mgr.state.active_capital_cents = 2200
    mgr.state.protected_capital_cents = 2200
    mgr.state.milestone_44_activated = True

    # +$10 net realized profit
    mgr.process_settlement(settlement_id="set-prof-1", net_pnl_usd=10.0)
    assert mgr.state.active_capital == 27.0
    assert mgr.state.protected_capital == 27.0
    assert mgr.state.total_equity == 54.0

    # Another +$10 profit
    mgr.process_settlement(settlement_id="set-prof-2", net_pnl_usd=10.0)
    assert mgr.state.active_capital == 32.0
    assert mgr.state.protected_capital == 32.0
    assert mgr.state.total_equity == 64.0


def test_post_milestone_loss_deduction(tmp_path: Path):
    """Section 1.4: Realized trading losses deduct from active capital ONLY."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=44.0)
    mgr.state.active_capital_cents = 2700
    mgr.state.protected_capital_cents = 2700
    mgr.state.total_equity_cents = 5400
    mgr.state.milestone_44_activated = True

    # -$5 realized loss
    mgr.process_settlement(settlement_id="set-loss-1", net_pnl_usd=-5.0)
    assert mgr.state.active_capital == 22.0
    assert mgr.state.protected_capital == 27.0  # Protected capital untouched!
    assert mgr.state.total_equity == 49.0


def test_option_b_next_day_reserve_activation(tmp_path: Path):
    """
    Section 1.6: Option B next-day reserve release.
    Active $30, Protected $110 -> 50% release of protected ($55) added to active ($85 active, $55 protected).
    """
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=140.0)
    mgr.state.active_capital_cents = 3000
    mgr.state.protected_capital_cents = 11000
    mgr.state.total_equity_cents = 14000
    mgr.state.milestone_44_activated = True

    day1 = "2026-11-01"
    day2 = "2026-11-02"

    # Daily shutdown occurs on Day 1
    event_id = mgr.record_daily_shutdown_qualifying_event(
        trading_day=day1,
        failed_initial_count=1,
        failed_reversal_count=1,
    )
    assert mgr.state.daily_shutdown_status
    assert mgr.state.next_day_activation_eligible

    # Attempt activation on same day -> rejected (must be next trading day)
    ok_same, _, _ = mgr.check_and_activate_next_day_reserve(current_trading_day=day1)
    assert not ok_same

    # Activate on next trading day (Day 2) -> SUCCESS
    ok_next, released_usd, msg = mgr.check_and_activate_next_day_reserve(current_trading_day=day2)
    assert ok_next
    assert released_usd == 55.0
    assert mgr.state.active_capital == 85.0
    assert mgr.state.protected_capital == 55.0
    assert mgr.state.total_equity == 140.0  # Invariant: Total equity unchanged!
    assert not mgr.state.next_day_activation_eligible
    assert not mgr.state.daily_shutdown_status

    # Budgets at $85 active: Initial round(85/11)=$8, Reversal round(850/11)=$77
    init_b, rev_b = mgr.get_trade_budgets()
    assert init_b == 8
    assert rev_b == 77

    # Attempt duplicate activation on same event -> rejected (idempotent / exactly once)
    ok_dup, _, _ = mgr.check_and_activate_next_day_reserve(current_trading_day=day2)
    assert not ok_dup


def test_repeated_qualifying_shutdown(tmp_path: Path):
    """Section 1.6: Repeated qualifying shutdown with $55 protected releases $27.50."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=140.0)
    mgr.state.active_capital_cents = 8500
    mgr.state.protected_capital_cents = 5500
    mgr.state.total_equity_cents = 14000
    mgr.state.milestone_44_activated = True

    day2 = "2026-11-02"
    day3 = "2026-11-03"

    mgr.record_daily_shutdown_qualifying_event(day2, 1, 1)
    ok, released, _ = mgr.check_and_activate_next_day_reserve(day3)
    assert ok
    assert released == 27.50
    assert mgr.state.active_capital == 112.50
    assert mgr.state.protected_capital == 27.50
    assert mgr.state.total_equity == 140.0


def test_settlement_idempotency(tmp_path: Path):
    """Section 4: A settlement can affect capital only once."""
    db_path = tmp_path / "test_capital.db"
    mgr = CapitalManager(db_path=db_path, starting_equity_usd=11.0)

    mgr.process_settlement(settlement_id="set-dup-1", net_pnl_usd=5.0)
    assert mgr.state.active_capital == 16.0

    # Repeat same settlement
    mgr.process_settlement(settlement_id="set-dup-1", net_pnl_usd=5.0)
    assert mgr.state.active_capital == 16.0  # Not 21.0!


def test_restart_durability(tmp_path: Path):
    """Section 7: Balances, milestone status, and transaction history survive restarts."""
    db_path = tmp_path / "test_capital.db"
    mgr1 = CapitalManager(db_path=db_path, starting_equity_usd=11.0)
    mgr1.process_settlement(settlement_id="set-p1", net_pnl_usd=33.0)  # Reaches $44
    mgr1.process_settlement(settlement_id="set-p2", net_pnl_usd=10.0)  # $27 active / $27 protected

    assert mgr1.state.active_capital == 27.0
    assert mgr1.state.protected_capital == 27.0

    # Reconnect new manager instance to same SQLite DB
    mgr2 = CapitalManager(db_path=db_path)
    assert mgr2.state.active_capital == 27.0
    assert mgr2.state.protected_capital == 27.0
    assert mgr2.state.total_equity == 54.0
    assert mgr2.state.milestone_44_activated

    # Check transactions audit log
    txs = mgr2.ledger.get_recent_transactions(limit=10)
    assert len(txs) >= 3
