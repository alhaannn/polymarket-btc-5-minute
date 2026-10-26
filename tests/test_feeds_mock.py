import json
from polybot.feeds.binance import BinanceFeed
from polybot.feeds.clob_ws import CLOBOrderBookFeed
from polybot.feeds.rtds import RTDSFeed


def test_rtds_message_processing():
    ticks = []
    feed = RTDSFeed(on_tick=lambda t: ticks.append(t))

    # Real message recorded from RTDS
    sample_rtds_payload = {
        "connection_id": "test-conn-1",
        "payload": {
            "full_accuracy_value": "80266841765283890000000",
            "symbol": "btc/usd",
            "timestamp": 1793037788000,
            "value": 80266.84176528388,
        },
        "timestamp": 1793037789919,
        "topic": "crypto_prices_chainlink",
    }

    feed._process_message(json.dumps(sample_rtds_payload))
    assert len(ticks) == 1
    t = ticks[0]
    assert t["feed"] == "chainlink"
    assert t["price"] == 80266.84176528388
    assert t["timestamp_ms"] == 1793037788000


def test_binance_message_processing():
    ticks = []
    feed = BinanceFeed(on_tick=lambda t: ticks.append(t))

    sample_binance_payload = {
        "e": "trade",
        "E": 1793037547086,
        "s": "BTCUSDT",
        "p": "80358.10000000",
        "q": "0.002",
        "T": 1793037547474,
    }

    feed._process_message(json.dumps(sample_binance_payload))
    assert len(ticks) == 1
    t = ticks[0]
    assert t["feed"] == "binance"
    assert t["price"] == 80358.10
    assert t["timestamp_ms"] == 1793037547474


def test_clob_ws_message_processing():
    feed = CLOBOrderBookFeed()
    feed.track_tokens({"Up": "token-up-123", "Down": "token-down-456"})

    # Snapshot message
    snapshot = [
        {
            "asset_id": "token-up-123",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "150"}],
        },
        {
            "asset_id": "token-down-456",
            "bids": [{"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.51", "size": "120"}],
        },
    ]
    feed._process_message(json.dumps(snapshot))

    up_book = feed.get_book("Up")
    down_book = feed.get_book("Down")

    assert up_book is not None
    assert up_book.best_bid() == 0.48
    assert up_book.best_ask() == 0.52

    assert down_book is not None
    assert down_book.best_bid() == 0.47
    assert down_book.best_ask() == 0.51

    # Delta message
    delta = {
        "price_changes": [
            {"asset_id": "token-up-123", "price": "0.50", "size": "60", "side": "BUY"}
        ]
    }
    feed._process_message(json.dumps(delta))
    assert up_book.best_bid() == 0.50
