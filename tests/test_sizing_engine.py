"""
Unit tests for Dynamic Trade-Sizing Engine (Roadmap Phase 3).
Validates whole-dollar rounding, boundary conditions, free active funds isolation,
open exposure reservations, fee buffers, and strategy integration.
"""
import pytest
from polybot.capital.sizing import (
    DynamicTradeBudgetCalculator,
    round_half_up_to_whole_dollar,
)
from polybot.strategy import ReversalStrategy
from polybot.feeds.clob_ws import TokenOrderBook


def create_mock_books(up_ask: float, down_ask: float, depth: float = 500.0):
    book_up = TokenOrderBook(token_id="tok-up")
    book_down = TokenOrderBook(token_id="tok-down")
    book_up.asks = {up_ask: depth}
    book_up.bids = {round(1.0 - down_ask, 4): depth}
    book_down.asks = {down_ask: depth}
    book_down.bids = {round(1.0 - up_ask, 4): depth}
    return {"Up": book_up, "Down": book_down}


def test_round_half_up_boundary_conditions():
    """Verifies half-dollar ties round upward deterministically."""
    assert round_half_up_to_whole_dollar(0.49) == 0
    assert round_half_up_to_whole_dollar(0.50) == 1
    assert round_half_up_to_whole_dollar(0.51) == 1

    assert round_half_up_to_whole_dollar(1.49) == 1
    assert round_half_up_to_whole_dollar(1.50) == 2
    assert round_half_up_to_whole_dollar(1.51) == 2

    assert round_half_up_to_whole_dollar(9.49) == 9
    assert round_half_up_to_whole_dollar(9.50) == 10
    assert round_half_up_to_whole_dollar(9.51) == 10


def test_all_canonical_example_balances():
    """Verifies exact matches with Roadmap Section 1.2 and Phase 3 Table."""
    calc = DynamicTradeBudgetCalculator()

    canonical_cases = [
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

    for active, exp_init, exp_rev in canonical_cases:
        init_b, rev_b = calc.get_budgets(active)
        assert init_b == exp_init, f"Init budget mismatch for active=${active}: expected {exp_init}, got {init_b}"
        assert rev_b == exp_rev, f"Rev budget mismatch for active=${active}: expected {exp_rev}, got {rev_b}"


def test_zero_and_very_small_balances():
    """Verify zero or depleted active capital rejects order without touching protected capital."""
    calc = DynamicTradeBudgetCalculator(min_order_usd=1.0)

    # 1. Zero balance
    init_b, rev_b = calc.get_budgets(0.0)
    assert init_b == 0
    assert rev_b == 0

    res_zero = calc.validate_order_budget(
        signal_type="INITIAL",
        active_capital_usd=0.0,
        open_exposure_usd=0.0,
    )
    assert not res_zero.allowed
    assert "zero or depleted" in res_zero.reason.lower()

    # 2. Very small balance ($2.00 active capital -> 2/11 = $0.18 -> rounds to $0)
    res_small = calc.validate_order_budget(
        signal_type="INITIAL",
        active_capital_usd=2.0,
        open_exposure_usd=0.0,
    )
    assert not res_small.allowed
    assert "below minimum" in res_small.reason.lower()


def test_negative_active_capital_raises_value_error():
    calc = DynamicTradeBudgetCalculator()
    with pytest.raises(ValueError):
        calc.get_budgets(-10.0)

    with pytest.raises(ValueError):
        calc.validate_order_budget("INITIAL", active_capital_usd=-5.0)


def test_open_position_exposure_reservations():
    """Verify committed open exposure is subtracted from active funds before order approval."""
    calc = DynamicTradeBudgetCalculator()

    # Active $22.00, but $20.00 already committed in open positions -> free active is only $2.00
    # Initial budget is $2.00 -> $2.00 + $0.002 fee = $2.002 > $2.00 free -> rejected
    res_tight = calc.validate_order_budget(
        signal_type="INITIAL",
        active_capital_usd=22.0,
        open_exposure_usd=20.00,
    )
    assert not res_tight.allowed
    assert "insufficient free active funds" in res_tight.reason.lower()

    # With open exposure $19.00 -> free active is $3.00 -> $2.00 fits -> approved!
    res_ok = calc.validate_order_budget(
        signal_type="INITIAL",
        active_capital_usd=22.0,
        open_exposure_usd=19.00,
    )
    assert res_ok.allowed
    assert res_ok.approved_budget_usd == 2.0


def test_reversal_budget_validation_with_exposure():
    """Verifies reversal budget ($10 on $11 active, $77 on $85 active)."""
    calc = DynamicTradeBudgetCalculator()

    # Active $85.00: Reversal budget is $77.00
    # If open exposure is $1.00 (from initial order) -> free active is $84.00
    # $77.00 + $0.077 fee = $77.077 <= $84.00 -> approved!
    res_rev = calc.validate_order_budget(
        signal_type="REVERSAL",
        active_capital_usd=85.0,
        open_exposure_usd=1.0,
    )
    assert res_rev.allowed
    assert res_rev.approved_budget_usd == 77.0

    # If open exposure was $15.00 -> free active is $70.00 < $77.077 -> rejected!
    res_rev_blocked = calc.validate_order_budget(
        signal_type="REVERSAL",
        active_capital_usd=85.0,
        open_exposure_usd=15.0,
    )
    assert not res_rev_blocked.allowed
    assert "insufficient free active funds" in res_rev_blocked.reason.lower()


def test_strategy_dynamic_budget_injection():
    """Verifies ReversalStrategy executes exact dynamic stakes when set."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    # Set dynamic budgets derived from $85 active capital ($8 initial, $77 reversal)
    strat.set_dynamic_budgets(initial_usd=8.0, reversal_usd=77.0)

    # Trigger Initial order (Up @ 0.90)
    books = create_mock_books(up_ask=0.90, down_ask=0.10, depth=1000.0)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books)
    assert sig_init is not None
    assert sig_init.signal_type == "INITIAL"
    assert pytest.approx(sig_init.cost_usd, rel=1e-3) == 8.0  # Dynamic $8 budget!

    # Trigger Reversal order (Down @ 0.88)
    books_rev = create_mock_books(up_ask=0.12, down_ask=0.88, depth=1000.0)
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1210.0, books=books_rev)
    assert sig_rev is not None
    assert sig_rev.signal_type == "REVERSAL"
    assert pytest.approx(sig_rev.cost_usd, rel=1e-3) == 77.0  # Dynamic $77 budget!


def test_adaptive_clamping_prevents_reversal_block_user_case():
    """
    Direct regression test for user scenario:
    Active capital: $19.38
    Initial order open exposure: $2.00
    Canonical reversal: $18.00 (requires $18.018 > $17.38 free)
    Adaptive clamping must approve $17.00 without blocking.
    """
    calc = DynamicTradeBudgetCalculator()

    # 1. Canonical budgets
    init_b, rev_b = calc.get_budgets(19.38)
    assert init_b == 2
    assert rev_b == 18

    # 2. Executable reversal budget derivation
    exec_rev = calc.get_executable_reversal_budget(active_capital_usd=19.38, open_exposure_usd=2.00)
    assert exec_rev == 17  # $17.00 fits cleanly in $17.38 free funds with taker fees

    # 3. Without adaptive clamping -> rejected
    res_strict = calc.validate_order_budget(
        signal_type="REVERSAL",
        active_capital_usd=19.38,
        open_exposure_usd=2.00,
        allow_adaptive_clamping=False,
    )
    assert not res_strict.allowed
    assert "insufficient free active funds" in res_strict.reason.lower()

    # 4. With adaptive clamping -> approved at $17.00!
    res_adaptive = calc.validate_order_budget(
        signal_type="REVERSAL",
        active_capital_usd=19.38,
        open_exposure_usd=2.00,
        allow_adaptive_clamping=True,
    )
    assert res_adaptive.allowed
    assert res_adaptive.approved_budget_usd == 17.0
    assert res_adaptive.calculated_budget_usd == 18.0
    assert "clamped to available free active balance" in res_adaptive.reason.lower()

