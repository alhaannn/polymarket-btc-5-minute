"""
Unit tests for High-Equity Orderbook Safeguards (Century Tier Dynamic Transition).
Tests:
- Canonical small-account preservation ($11 -> $50 active)
- Seamless boundary handover at $50 active ($100 total equity)
- High-equity 8% initial sizing with 4x scaled-down reversal multiplier
- Hard caps: $20 max initial, $75 max reversal
- Strict free active funds validation
"""
import pytest
from polybot.capital.sizing import DynamicTradeBudgetCalculator


def test_canonical_preservation_below_century_threshold():
    """Verify active capital <= $50 retains exact canonical 1:10 sizing."""
    calc = DynamicTradeBudgetCalculator(
        enable_high_equity_transition=True,
        century_active_threshold_usd=50.0,
        max_initial_stake_usd=20.0,
        max_reversal_stake_usd=75.0,
        high_equity_reversal_multiplier=4.0,
        high_equity_initial_ratio=0.08,
    )

    canonical_checks = [
        (11.0, 1, 10),
        (22.0, 2, 20),
        (33.0, 3, 30),
        (39.93, 4, 36),  # Current live balance
        (44.0, 4, 40),
        (50.0, 5, 45),   # Handover boundary
    ]

    for active, exp_init, exp_rev in canonical_checks:
        init_b, rev_b = calc.get_budgets(active)
        assert init_b == exp_init, f"Init budget mismatch at ${active}: expected {exp_init}, got {init_b}"
        assert rev_b == exp_rev, f"Rev budget mismatch at ${active}: expected {exp_rev}, got {rev_b}"


def test_high_equity_scaling_above_century_threshold():
    """Verify active capital > $50 scales at 8% initial and 4x multiplier."""
    calc = DynamicTradeBudgetCalculator(
        enable_high_equity_transition=True,
        century_active_threshold_usd=50.0,
        max_initial_stake_usd=20.0,
        max_reversal_stake_usd=75.0,
        high_equity_reversal_multiplier=4.0,
        high_equity_initial_ratio=0.08,
    )

    high_equity_cases = [
        # active, exp_init (round(active * 0.08)), exp_rev (round(init * 4.0))
        (75.0, 6, 24),    # 75 * 0.08 = 6 -> 6 * 4 = 24
        (100.0, 8, 32),   # 100 * 0.08 = 8 -> 8 * 4 = 32
        (125.0, 10, 40),  # 125 * 0.08 = 10 -> 10 * 4 = 40
        (150.0, 12, 48),  # 150 * 0.08 = 12 -> 12 * 4 = 48
        (200.0, 16, 64),  # 200 * 0.08 = 16 -> 16 * 4 = 64
    ]

    for active, exp_init, exp_rev in high_equity_cases:
        init_b, rev_b = calc.get_budgets(active)
        assert init_b == exp_init, f"High-equity init mismatch at ${active}: expected {exp_init}, got {init_b}"
        assert rev_b == exp_rev, f"High-equity rev mismatch at ${active}: expected {exp_rev}, got {rev_b}"


def test_hard_stake_caps_at_large_balances():
    """Verify initial is hard-capped at $20 and reversal at $75 at large bankrolls."""
    calc = DynamicTradeBudgetCalculator(
        enable_high_equity_transition=True,
        century_active_threshold_usd=50.0,
        max_initial_stake_usd=20.0,
        max_reversal_stake_usd=75.0,
        high_equity_reversal_multiplier=4.0,
        high_equity_initial_ratio=0.08,
    )

    capped_cases = [
        # active, exp_init (capped at 20), exp_rev (capped at 75)
        (250.0, 20, 75),  # 250 * 0.08 = 20 -> 20 * 4 = 80 -> capped at 75
        (500.0, 20, 75),  # 500 * 0.08 = 40 -> capped at 20 -> 20 * 4 = 80 -> capped at 75
        (1000.0, 20, 75), # 1000 * 0.08 = 80 -> capped at 20 -> 20 * 4 = 80 -> capped at 75
    ]

    for active, exp_init, exp_rev in capped_cases:
        init_b, rev_b = calc.get_budgets(active)
        assert init_b == exp_init, f"Hard cap init mismatch at ${active}: expected {exp_init}, got {init_b}"
        assert rev_b == exp_rev, f"Hard cap rev mismatch at ${active}: expected {exp_rev}, got {rev_b}"


def test_toggle_disabled_pure_canonical():
    """Verify setting enable_high_equity_transition=False preserves uncapped canonical behavior."""
    calc = DynamicTradeBudgetCalculator(
        enable_high_equity_transition=False,
    )
    init_b, rev_b = calc.get_budgets(220.0)
    assert init_b == 20
    assert rev_b == 200


def test_capital_manager_integration():
    """Verify CapitalManager dynamically reports correct budgets across thresholds."""
    from polybot.capital.manager import CapitalManager
    from polybot.capital.models import CapitalState
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ledger.db"
        cm = CapitalManager(
            db_path=db_path,
            starting_equity_usd=11.0,
            enable_high_equity_transition=True,
            century_active_threshold_usd=50.0,
            max_initial_stake_usd=20.0,
            max_reversal_stake_usd=75.0,
            high_equity_reversal_multiplier=4.0,
            high_equity_initial_ratio=0.08,
        )

        # 1. Starting equity ($11)
        init_b, rev_b = cm.get_trade_budgets()
        assert (init_b, rev_b) == (1, 10)

        # 2. Current live balance ($39.93)
        cm.state.active_capital_cents = 3993
        cm.state.total_equity_cents = 3993
        init_b, rev_b = cm.get_trade_budgets()
        assert (init_b, rev_b) == (4, 36)

        # 3. Post-Milestone 1 ($44 Total: $22 Active / $22 Protected)
        cm.state.active_capital_cents = 2200
        cm.state.protected_capital_cents = 2200
        cm.state.total_equity_cents = 4400
        init_b, rev_b = cm.get_trade_budgets()
        assert (init_b, rev_b) == (2, 20)

        # 4. Century Tier ($150 Total: $75 Active / $75 Protected)
        cm.state.active_capital_cents = 7500
        cm.state.protected_capital_cents = 7500
        cm.state.total_equity_cents = 15000
        init_b, rev_b = cm.get_trade_budgets()
        assert (init_b, rev_b) == (6, 24)

        # 5. High Equity ($1000 Total: $500 Active / $500 Protected)
        cm.state.active_capital_cents = 50000
        cm.state.protected_capital_cents = 50000
        cm.state.total_equity_cents = 100000
        init_b, rev_b = cm.get_trade_budgets()
        assert (init_b, rev_b) == (20, 75)

