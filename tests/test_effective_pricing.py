import pytest
from polybot.feeds.clob_ws import CLOBOrderBookFeed, TokenOrderBook
from polybot.strategy import ReversalStrategy, StrategyConfig


def test_effective_pricing_complement():
    feed = CLOBOrderBookFeed()
    feed.track_tokens({"Up": "tok-up-1", "Down": "tok-down-1"})

    # Suppose tok-down has bids from 0.01 up to 0.88, but no asks on tok-up
    book_down = feed.get_book("Down")
    book_down.apply_snapshot([{"price": "0.88", "size": "100.0"}], [])

    book_up = feed.get_book("Up")
    book_up.apply_snapshot([], [])

    # Effective ask on Up should be 1.0 - 0.88 = 0.12
    assert feed.get_effective_ask("Up") == 0.12
    assert feed.get_effective_bid("Down") == 0.88

    # Side books property maps correctly
    sb = feed.side_books
    assert "Up" in sb
    assert "Down" in sb
    assert sb["Up"] is book_up
    assert sb["Down"] is book_down


def test_reversal_strategy_with_complementary_prices():
    config = StrategyConfig(
        reversal_window_start_sec=180.0,
        reversal_window_end_sec=300.0,
        initial_prob_min=0.89,
        initial_prob_max=0.92,
        initial_stake_usd=1.0,
        reversal_prob_min=0.85,
        reversal_prob_max=0.90,
        reversal_stake_usd=10.0,
    )
    strat = ReversalStrategy(config)
    strat.reset_window(1000, 68000.0)

    # Market where Down has a bid at 0.10, so Up implied ask is 0.90 (in 89%-92% range)
    feed = CLOBOrderBookFeed()
    feed.track_tokens({"Up": "tok-up", "Down": "tok-down"})
    book_down = feed.get_book("Down")
    book_down.apply_snapshot([{"price": "0.10", "size": "500"}], [])

    # Trigger initial signal at 185s
    sig = strat.on_tick(68050.0, 1185.0, feed.side_books)
    assert sig is not None
    assert sig.signal_type == "INITIAL"
    assert sig.side == "Up"
    assert sig.price == pytest.approx(0.90)
    assert sig.cost_usd == pytest.approx(1.0)

