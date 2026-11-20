import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy


def create_mock_books(up_ask: float, down_ask: float):
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    book_up.apply_snapshot(
        [{"price": str(round(up_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(up_ask), "size": "100"}]
    )
    book_down.apply_snapshot(
        [{"price": str(round(down_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(down_ask), "size": "100"}]
    )
    return {"Up": book_up, "Down": book_down}


def test_initial_only_entry_and_zero_reversals():
    """Initial-Only (Maximum Safety): Takes initial trade at 90c, zero reversals taken even on severe adverse price move."""
    cfg = StrategyConfig(
        strategy_name="reversal",
        initial_stake_usd=1.00,
        reversal_stake_usd=0.00,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        early_exit=False,
        enable_reversals=False,
        max_reversal_stages=0,
        prime_hours_only=False,
        entry_start_sec=180.0,
        entry_end_sec=210.0,
    )
    strat = ReversalStrategy(cfg)
    strat.reset_window(window_start=1000, strike_price_K=65000.0)

    # 1. At 100s, no entry
    books_100 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1100.0,
        books=books_100,
    )
    assert sig is None

    # 2. At 190s (inside 180-210s window), Up is 0.90 -> Initial entry triggers
    books_190 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig = strat.on_tick(
        current_price=65150.0,
        timestamp_sec=1190.0,
        books=books_190,
    )
    assert sig is not None
    assert sig.signal_type == "INITIAL"
    assert sig.side == "Up"
    assert pytest.approx(sig.price, rel=1e-3) == 0.90
    assert sig.cost_usd == pytest.approx(1.00, abs=0.01)

    # 3. Market reverses severely: Up crashes to 0.10, Down explodes to 0.90 at 240s
    books_rev = create_mock_books(up_ask=0.10, down_ask=0.90)
    for t in [1220.0, 1240.0, 1260.0, 1280.0]:
        sig_rev = strat.on_tick(
            current_price=64800.0,
            timestamp_sec=t,
            books=books_rev,
        )
        # ZERO reversals should be placed!
        assert sig_rev is None, f"Expected None but got reversal signal {sig_rev} at {t}"


def test_initial_only_5_dollar_stake_and_prime_hours():
    """Initial-Only with $5.00 stake and restricted prime hours only."""
    cfg = StrategyConfig(
        strategy_name="reversal",
        initial_stake_usd=5.00,
        reversal_stake_usd=0.00,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        early_exit=False,
        enable_reversals=False,
        max_reversal_stages=0,
        prime_hours_only=True,
        entry_start_sec=180.0,
        entry_end_sec=210.0,
    )
    strat = ReversalStrategy(cfg)
    strat.set_dynamic_budgets(initial_usd=5.0, reversal_usd=0.0)

    # 1. Non-prime hour: 06:00 IST (timestamp 1795134600.0)
    strat.reset_window(window_start=1795134600, strike_price_K=65000.0)
    books_np = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_np = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1795134600.0 + 190.0,
        books=books_np,
    )
    # Blocked by restricted prime hours
    assert sig_np is None

    # 2. Prime hour: 15:00 IST (timestamp 1795167000)
    strat.reset_window(window_start=1795167000, strike_price_K=65000.0)
    strat.set_dynamic_budgets(initial_usd=5.0, reversal_usd=0.0)
    books_prime = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_prime = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1795167000.0 + 190.0,
        books=books_prime,
    )
    assert sig_prime is not None
    assert sig_prime.signal_type == "INITIAL"
    assert pytest.approx(sig_prime.cost_usd, abs=0.05) == 5.00
