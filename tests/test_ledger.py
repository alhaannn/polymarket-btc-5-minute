import pytest
from polybot.execution.base import Order
from polybot.execution.ledger import TradeLedger
from polybot.execution.paper import PaperExecutionEngine
from polybot.feeds.clob_ws import TokenOrderBook


def test_trade_ledger_lifecycle_and_persistence(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    ledger = TradeLedger(db_file)

    # Record Initial trade
    ledger.record_trade(
        order_id="ord-init-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        signal_type="INITIAL",
        stake_usd=1.0,
        shares=1.1111,
        avg_price=0.90,
        fee_usd=0.001,
        exec_ts=1000.0,
        trigger_prob=0.90,
        counterpart_prob=0.10,
        status="OPEN",
    )

    # Record Reversal trade
    ledger.record_trade(
        order_id="ord-rev-1",
        slug="btc-updown-5m-1000",
        side="Down",
        action="BUY",
        order_type="taker",
        signal_type="REVERSAL",
        linked_order_id="ord-init-1",
        stake_usd=10.0,
        shares=11.494,
        avg_price=0.87,
        fee_usd=0.01,
        exec_ts=1010.0,
        trigger_prob=0.87,
        counterpart_prob=0.13,
        status="OPEN",
    )

    open_trades = ledger.get_open_trades()
    assert len(open_trades) == 2

    # Settle the market where Down wins
    # Initial Up loses: payout = $0.00, net pnl = 0 - 1.0 - 0.001 = -1.001
    # Reversal Down wins: payout = $11.494, net pnl = 11.494 - 10.0 - 0.01 = +1.484
    settled = ledger.settle_slug("btc-updown-5m-1000", winner="Down", settled_at_ts=1300.0)
    assert len(settled) == 2

    # Verify no open trades remain
    assert len(ledger.get_open_trades()) == 0

    # Test persistence by opening a new connection to the same file
    ledger2 = TradeLedger(db_file)
    recent = ledger2.get_recent_trades()
    assert len(recent) == 2

    summary = ledger2.get_performance_summary()
    assert summary["lifetime"]["total_trades"] == 2
    assert summary["lifetime"]["initial_trades"] == 1
    assert summary["lifetime"]["reversal_trades"] == 1
    assert summary["lifetime"]["wins"] == 1
    assert summary["lifetime"]["losses"] == 1
    assert summary["lifetime"]["win_rate_pct"] == 50.0
    assert summary["lifetime"]["net_pnl_usd"] > 0  # Reversal win covered initial loss


def test_paper_engine_with_initial_and_reversal_pair(tmp_path):
    db_file = tmp_path / "engine_test_ledger.db"
    ledger = TradeLedger(db_file)
    engine = PaperExecutionEngine(ledger=ledger)

    book_up = TokenOrderBook("tok-up")
    book_up.apply_snapshot([], [{"price": "0.90", "size": "100"}])
    book_down = TokenOrderBook("tok-down")
    book_down.apply_snapshot([], [{"price": "0.88", "size": "100"}])
    books = {"Up": book_up, "Down": book_down}

    # 1. Initial order: Up $1.00
    ord1 = Order(
        order_id="ord-init-100",
        slug="btc-updown-5m-2000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=1.0,
        shares=0.0,
        created_at_ts=2000.0,
        latency_ms=0,
        signal_type="INITIAL",
        trigger_prob=0.90,
    )
    engine.submit_order(ord1)
    fills1 = engine.process_pending_orders(2000.0, books)
    assert len(fills1) == 1

    # 2. Reversal order: Down $10.00 linked to ord1
    ord2 = Order(
        order_id="ord-rev-100",
        slug="btc-updown-5m-2000",
        side="Down",
        action="BUY",
        order_type="taker",
        price=0.88,
        usd_amount=10.0,
        shares=0.0,
        created_at_ts=2010.0,
        latency_ms=0,
        signal_type="REVERSAL",
        linked_order_id="ord-init-100",
        trigger_prob=0.88,
    )
    engine.submit_order(ord2)
    fills2 = engine.process_pending_orders(2010.0, books)
    assert len(fills2) == 1

    # Both positions must be open simultaneously
    assert len(engine.open_positions_list) == 2

    # Settle market at resolution: Down wins
    engine.settle_window("btc-updown-5m-2000", winner="Down", settle_ts=2300.0)
    assert len(engine.open_positions_list) == 0
    assert len(engine.settled_positions) == 2

    # Check ledger
    perf = ledger.get_performance_summary()
    assert perf["lifetime"]["total_trades"] == 2
    assert perf["lifetime"]["reversal_trades"] == 1
