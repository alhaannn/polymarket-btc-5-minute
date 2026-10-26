import time
import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.feeds.health import FeedHealthTracker
from polybot.strategy import EdgeStrategy, LegacyStrategy


def test_legacy_strategy():
    cfg = StrategyConfig(
        entry_start_sec=180.0,
        entry_end_sec=285.0,
        legacy_price_min=0.93,
        legacy_price_max=0.96,
        stake_usd=5.0,
    )
    strat = LegacyStrategy(cfg)
    strat.reset_window(window_start=1000, strike_price_K=80000.0)

    book_up = TokenOrderBook("token-up")
    book_down = TokenOrderBook("token-down")
    books = {"Up": book_up, "Down": book_down}

    # Setup ask at 0.94 (inside [0.93, 0.96])
    book_up.apply_snapshot([{"price": "0.90", "size": "100"}], [{"price": "0.94", "size": "100"}])
    book_down.apply_snapshot([{"price": "0.02", "size": "100"}], [{"price": "0.06", "size": "100"}])

    # 1. Before entry start (t=1100 -> elapsed 100s < 180s) -> No trade
    sig1 = strat.on_tick(current_price=80050.0, timestamp_sec=1100.0, books=books)
    assert sig1 is None

    # 2. Inside entry window (t=1200 -> elapsed 200s) -> Takes trade on Up
    sig2 = strat.on_tick(current_price=80050.0, timestamp_sec=1200.0, books=books)
    assert sig2 is not None
    assert sig2.side == "Up"
    assert sig2.action == "BUY"
    assert pytest.approx(sig2.price) == 0.94
    assert strat.has_traded

    # 3. Subsequent tick -> No duplicate trade (one trade per window rule)
    sig3 = strat.on_tick(current_price=80060.0, timestamp_sec=1210.0, books=books)
    assert sig3 is None


def test_edge_strategy_entry_and_early_exit():
    cfg = StrategyConfig(
        entry_start_sec=120.0,
        entry_end_sec=285.0,
        price_min=0.85,
        price_max=0.985,
        min_edge=0.02,
        z_min=0.5,
        min_lead_usd=5.0,
        early_exit=True,
        exit_p=0.40,
        stake_usd=5.0,
    )
    strat = EdgeStrategy(cfg)
    strat.reset_window(window_start=1000, strike_price_K=80000.0)

    # Prime volatility
    for i in range(15):
        strat.ewma_vol.update(80000.0 + i * 2, 1000.0 + i)

    health = FeedHealthTracker()
    health.record_tick("chainlink", 80100.0, 1150000)

    book_up = TokenOrderBook("token-up")
    book_down = TokenOrderBook("token-down")
    books = {"Up": book_up, "Down": book_down}

    # Up book has tight spread, ask @ 0.90
    book_up.apply_snapshot([{"price": "0.89", "size": "100"}], [{"price": "0.90", "size": "100"}])
    book_down.apply_snapshot([{"price": "0.08", "size": "100"}], [{"price": "0.10", "size": "100"}])

    # Price at 80100 is +$100 lead with 150s elapsed (150s left)
    # Model probability will be very high (> 0.95), so edge = 0.95 - 0.90 - fee > 0.02
    sig = strat.on_tick(current_price=80100.0, timestamp_sec=1150.0, books=books, health=health)
    assert sig is not None
    assert sig.side == "Up"
    assert sig.action == "BUY"
    assert strat.has_traded

    # Test Early Exit:
    # Price crashes to 79900 (-$100 lead), so Up probability collapses
    # Set bids on Up book so exit sell can fill
    book_up.apply_snapshot([{"price": "0.15", "size": "50"}], [{"price": "0.20", "size": "50"}])
    exit_sig = strat.on_tick(current_price=79900.0, timestamp_sec=1200.0, books=books, health=health)
    assert exit_sig is not None
    assert exit_sig.side == "Up"
    assert exit_sig.action == "SELL"
    assert pytest.approx(exit_sig.price) == 0.15
