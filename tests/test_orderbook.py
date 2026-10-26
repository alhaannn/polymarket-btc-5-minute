import pytest
from polybot.feeds.clob_ws import TokenOrderBook


def test_orderbook_snapshot_and_sorting():
    book = TokenOrderBook("test-token-1")
    bids = [{"price": "0.40", "size": "100"}, {"price": "0.45", "size": "50"}]
    asks = [{"price": "0.55", "size": "80"}, {"price": "0.50", "size": "60"}]

    book.apply_snapshot(bids, asks)

    sorted_bids = book.get_sorted_bids()
    assert sorted_bids[0] == (0.45, 50.0)
    assert sorted_bids[1] == (0.40, 100.0)

    sorted_asks = book.get_sorted_asks()
    assert sorted_asks[0] == (0.50, 60.0)
    assert sorted_asks[1] == (0.55, 80.0)

    assert book.best_bid() == 0.45
    assert book.best_ask() == 0.50
    assert pytest.approx(book.spread()) == 0.05


def test_orderbook_delta_update_and_removal():
    book = TokenOrderBook("test-token-1")
    book.apply_snapshot([], [{"price": "0.90", "size": "10"}])

    # Insert new ask
    book.apply_delta(price=0.88, size=15.0, side="SELL")
    assert book.best_ask() == 0.88

    # Remove ask by size 0
    book.apply_delta(price=0.88, size=0.0, side="SELL")
    assert book.best_ask() == 0.90


def test_orderbook_simulate_buy_fill():
    book = TokenOrderBook("test-token-1")
    # Ask 1: 10 shares @ 0.50 ($5.00)
    # Ask 2: 20 shares @ 0.60 ($12.00)
    asks = [{"price": "0.50", "size": "10"}, {"price": "0.60", "size": "20"}]
    book.apply_snapshot([], asks)

    # Buy with $5.00 -> should take exactly 10 shares @ 0.50
    fill = book.simulate_buy(usd=5.0)
    assert fill is not None
    avg_price, shares, spent = fill
    assert pytest.approx(avg_price) == 0.50
    assert pytest.approx(shares) == 10.0
    assert pytest.approx(spent) == 5.0

    # Buy with $8.00 -> takes 10 shares @ 0.50 ($5.00) + 5 shares @ 0.60 ($3.00)
    # Total shares = 15, spent = $8.00 -> avg price = 8 / 15 = 0.5333
    fill2 = book.simulate_buy(usd=8.0)
    assert fill2 is not None
    avg_price2, shares2, spent2 = fill2
    assert pytest.approx(shares2) == 15.0
    assert pytest.approx(spent2) == 8.0
    assert pytest.approx(avg_price2) == 8.0 / 15.0

    # Buy with $100 -> insufficient depth ($5 + $12 = $17 max)
    assert book.simulate_buy(usd=100.0) is None


def test_orderbook_simulate_sell_fill():
    book = TokenOrderBook("test-token-1")
    # Bid 1: 10 shares @ 0.50 ($5.00)
    # Bid 2: 10 shares @ 0.40 ($4.00)
    bids = [{"price": "0.50", "size": "10"}, {"price": "0.40", "size": "10"}]
    book.apply_snapshot(bids, [])

    # Sell 5 shares -> should fill at 0.50, usd = 2.50
    fill = book.simulate_sell(shares_to_sell=5.0)
    assert fill is not None
    avg_price, shares, usd = fill
    assert pytest.approx(avg_price) == 0.50
    assert pytest.approx(shares) == 5.0
    assert pytest.approx(usd) == 2.50

    # Sell 15 shares -> 10 @ 0.50 ($5.00) + 5 @ 0.40 ($2.00) = $7.00
    fill2 = book.simulate_sell(shares_to_sell=15.0)
    assert fill2 is not None
    avg_price2, shares2, usd2 = fill2
    assert pytest.approx(shares2) == 15.0
    assert pytest.approx(usd2) == 7.00
    assert pytest.approx(avg_price2) == 7.0 / 15.0

    # Sell 30 shares -> depth only 20
    assert book.simulate_sell(shares_to_sell=30.0) is None
