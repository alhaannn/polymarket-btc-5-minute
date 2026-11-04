"""
Unit tests for Option B: Prime-Hours Fortress and Pure Early Exit strategy.
Verifies:
1. Fast-track compounding sizing tiers (sizing_mode="tiered")
2. Prime hours gate (13:30 to 01:00 IST)
3. Hard price ceiling (0.910)
4. Pure early exit (reversals disabled)
"""
import datetime
import pytest
from polybot.capital.sizing import DynamicTradeBudgetCalculator
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy


def create_mock_books(up_ask: float, down_ask: float, depth: float = 1000.0) -> dict:
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    book_up.apply_snapshot(
        [{"price": str(round(1.0 - down_ask - 0.01, 3)), "size": str(depth)}],
        [{"price": str(up_ask), "size": str(depth)}]
    )
    book_down.apply_snapshot(
        [{"price": str(round(1.0 - up_ask - 0.01, 3)), "size": str(depth)}],
        [{"price": str(down_ask), "size": str(depth)}]
    )
    return {"Up": book_up, "Down": book_down}


def test_tiered_sizing_mode():
    """Verify tiered fast-track sizing thresholds for compounding."""
    calc = DynamicTradeBudgetCalculator(sizing_mode="tiered")

    # < $15 -> $2
    assert calc.calculate_initial_budget(11.0) == 2
    assert calc.calculate_initial_budget(14.99) == 2

    # $15 - $25 -> $3
    assert calc.calculate_initial_budget(15.0) == 3
    assert calc.calculate_initial_budget(24.5) == 3

    # $25 - $35 -> $4
    assert calc.calculate_initial_budget(25.0) == 4
    assert calc.calculate_initial_budget(34.0) == 4

    # $35 - $50 -> $5
    assert calc.calculate_initial_budget(35.0) == 5
    assert calc.calculate_initial_budget(49.0) == 5

    # >= $50 -> min(8.0, round(active * 0.15))
    assert calc.calculate_initial_budget(50.0) == 8
    assert calc.calculate_initial_budget(100.0) == 8


def test_prime_hours_enforcement():
    """Verify that trades are allowed only between 13:30 and 01:00 IST."""
    cfg = StrategyConfig(prime_hours_only=True, enable_reversals=False, price_ceiling=0.910)
    strat = ReversalStrategy(config=cfg)

    tz_ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    # Test outside prime hours: 05:00 IST
    dt_outside = datetime.datetime(2026, 11, 4, 5, 0, 0, tzinfo=tz_ist)
    ts_outside = dt_outside.timestamp()
    strat.reset_window(window_start=ts_outside - 200.0)

    books = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_outside = strat.on_tick(current_price=80000.0, timestamp_sec=ts_outside, books=books)
    assert sig_outside is None, "Should not trade outside prime hours (05:00 IST)"

    # Test inside prime hours: 14:00 IST (2:00 PM)
    dt_inside = datetime.datetime(2026, 11, 4, 14, 0, 0, tzinfo=tz_ist)
    ts_inside = dt_inside.timestamp()
    strat.reset_window(window_start=ts_inside - 200.0)

    sig_inside = strat.on_tick(current_price=80000.0, timestamp_sec=ts_inside, books=books)
    assert sig_inside is not None, "Should trade inside prime hours (14:00 IST)"
    assert sig_inside.side == "Up"


def test_price_ceiling_filter():
    """Verify that entries above price_ceiling (0.910) are rejected."""
    cfg = StrategyConfig(prime_hours_only=False, enable_reversals=False, price_ceiling=0.910)
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 0.915 > 0.910 -> Should be blocked by price ceiling
    books_high = create_mock_books(up_ask=0.915, down_ask=0.085)
    sig_high = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_high)
    assert sig_high is None, "Should reject entry above price ceiling (0.915 > 0.910)"

    # 0.905 <= 0.910 -> Should be allowed
    books_valid = create_mock_books(up_ask=0.905, down_ask=0.095)
    sig_valid = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_valid)
    assert sig_valid is not None, "Should allow entry at or below price ceiling"
    assert sig_valid.side == "Up"


def test_pure_early_exit_no_reversals():
    """Verify that when enable_reversals=False, no reversal trades are fired."""
    cfg = StrategyConfig(prime_hours_only=False, enable_reversals=False, price_ceiling=0.910)
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 1. Trigger Initial trade
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)
    assert sig_init is not None
    assert strat.initial_order_placed

    # 2. Market flips (Down ask is 0.88, Up collapsed to 0.12)
    books_rev = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_rev)
    assert sig_rev is None, "Should NOT trigger reversal trade when enable_reversals=False"
