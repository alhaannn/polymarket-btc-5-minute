import pytest
from polybot.config import StrategyConfig
from polybot.execution.base import Order
from polybot.execution.fees import calculate_fee, FeeSchedule
from polybot.execution.paper import PaperExecutionEngine
from polybot.feeds.clob_ws import TokenOrderBook


def test_fee_calculation():
    schedule = FeeSchedule(taker_rate=0.001, maker_rate=0.000)
    # Taker fee: 100 shares @ 0.50 ($50) * 0.001 = $0.05
    fee_taker = calculate_fee(price=0.50, shares=100.0, order_type="taker", schedule=schedule)
    assert pytest.approx(fee_taker) == 0.05

    # Maker fee: 0%
    fee_maker = calculate_fee(price=0.50, shares=100.0, order_type="maker", schedule=schedule)
    assert pytest.approx(fee_maker) == 0.0


def test_paper_execution_latency_queue():
    cfg = StrategyConfig(latency_ms=400)
    engine = PaperExecutionEngine(cfg)

    book = TokenOrderBook("token-up")
    book.apply_snapshot([], [{"price": "0.90", "size": "100"}])
    books = {"Up": book}

    t0 = 1000.0
    order = Order(
        order_id="ord-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=5.0,
        shares=0.0,
        created_at_ts=t0,
        latency_ms=400,
    )
    engine.submit_order(order)
    assert len(engine.pending_orders) == 1

    # At t = 1000.2 (200ms < 400ms latency) -> should NOT fill yet
    fills_early = engine.process_pending_orders(current_ts=1000.2, books=books)
    assert len(fills_early) == 0
    assert len(engine.pending_orders) == 1

    # At t = 1000.41 (410ms >= 400ms latency) -> fills against the book
    fills_delayed = engine.process_pending_orders(current_ts=1000.41, books=books)
    assert len(fills_delayed) == 1
    assert len(engine.pending_orders) == 0
    f = fills_delayed[0]
    assert f.action == "BUY"
    assert pytest.approx(f.price) == 0.90
    assert pytest.approx(f.cost_usd) == 5.0


def test_paper_maker_order_and_timeout():
    cfg = StrategyConfig(latency_ms=0, maker_timeout_sec=5.0)
    engine = PaperExecutionEngine(cfg)

    book = TokenOrderBook("token-up")
    # Current best ask is 0.95 (above our limit of 0.90)
    book.apply_snapshot([{"price": "0.85", "size": "100"}], [{"price": "0.95", "size": "100"}])
    books = {"Up": book}

    t0 = 1000.0
    maker_order = Order(
        order_id="maker-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="maker",
        price=0.90,
        usd_amount=5.0,
        shares=0.0,
        created_at_ts=t0,
        latency_ms=0,
    )
    engine.submit_order(maker_order)

    # First check: market ask is 0.95 > 0.90 -> resting, no fill
    fills1 = engine.process_pending_orders(current_ts=1001.0, books=books)
    assert len(fills1) == 0
    assert len(engine.pending_orders) == 1

    # Market moves down: seller arrives with ask @ 0.90 (trades through our limit)
    book.apply_snapshot([{"price": "0.85", "size": "100"}], [{"price": "0.90", "size": "50"}])
    fills2 = engine.process_pending_orders(current_ts=1002.0, books=books)
    assert len(fills2) == 1
    assert fills2[0].price == 0.90
    assert len(engine.pending_orders) == 0


def test_position_settlement_pnl():
    engine = PaperExecutionEngine()
    book = TokenOrderBook("token-up")
    book.apply_snapshot([], [{"price": "0.90", "size": "100"}])
    books = {"Up": book}

    order = Order(
        order_id="ord-win",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=9.0,  # 10 shares @ 0.90
        shares=0.0,
        created_at_ts=1000.0,
        latency_ms=0,
    )
    engine.submit_order(order)
    engine.process_pending_orders(current_ts=1000.0, books=books)
    assert "btc-updown-5m-1000" in engine.open_positions

    # Up wins: 10 shares pay $10.00. Cost = $9.00, fee = $0.009 -> PnL ~= $0.991
    pos_win = engine.settle_window("btc-updown-5m-1000", winner="Up", settle_ts=1300.0)
    assert pos_win is not None
    assert pos_win.status == "SETTLED"
    assert pytest.approx(pos_win.payout_usd) == 10.0
    assert pytest.approx(pos_win.realized_pnl_usd, abs=0.02) == 0.991
    assert engine.get_total_realized_pnl() > 0
