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
    """Verify that when enable_reversals=False, early exit triggers a SELL at <=0.30 and NO reversal buy is fired."""
    cfg = StrategyConfig(prime_hours_only=False, enable_reversals=False, early_exit=True, exit_p=0.30, price_ceiling=0.910)
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 1. Trigger Initial trade
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)
    assert sig_init is not None
    assert sig_init.action == "BUY"
    assert strat.initial_order_placed

    # 2. Market flips (Down ask rises to 0.88, Up collapsed to 0.12 bid 0.11)
    books_rev = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_exit = strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_rev)
    assert sig_exit is not None
    assert sig_exit.action == "SELL", "Should execute SELL to cut losing trade early"
    assert sig_exit.signal_type == "EARLY_EXIT"
    assert sig_exit.side == "Up"

    # 3. Subsequent ticks: No reversal BUY fired
    sig_next = strat.on_tick(current_price=80000.0, timestamp_sec=1221.0, books=books_rev)
    assert sig_next is None, "Should NOT trigger reversal BUY when enable_reversals=False"


def test_early_exit_disabled_holds_position():
    """Verify that when early_exit=False, position is held without early exit sell."""
    cfg = StrategyConfig(prime_hours_only=False, enable_reversals=False, early_exit=False, price_ceiling=0.910)
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)
    assert sig_init is not None

    books_rev = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_rev)
    assert sig_rev is None, "Should hold position when early_exit=False"


def test_early_exit_paper_execution_and_settlement(tmp_path):
    """Verify that PaperExecutionEngine executes early exit SELL and settles with salvaged funds."""
    from polybot.execution.ledger import TradeLedger
    from polybot.execution.base import Order
    from polybot.execution.paper import PaperExecutionEngine

    db_path = tmp_path / "test_ledger.db"
    ledger = TradeLedger(db_path)
    cfg = StrategyConfig(latency_ms=100, strategy_name="reversal")
    engine = PaperExecutionEngine(cfg, ledger=ledger)

    book = TokenOrderBook("tok-up")
    book.apply_snapshot(
        [{"price": "0.30", "size": "100"}],
        [{"price": "0.90", "size": "100"}]
    )
    books = {"Up": book}

    # 1. Submit BUY order ($1.80 for 2.0 shares @ 0.90)
    buy_order = Order(
        order_id="ord-buy-1",
        slug="btc-5m-test",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=1.80,
        shares=2.0,
        created_at_ts=1000.0,
        latency_ms=100,
        signal_type="INITIAL",
    )
    engine.submit_order(buy_order)
    fills = engine.process_pending_orders(1000.15, books)
    assert len(fills) == 1
    assert fills[0].action == "BUY"
    assert len(engine.open_positions) == 1

    # 2. Market collapses: bid is 0.30, sell 2.0 shares
    sell_order = Order(
        order_id="ord-sell-1",
        slug="btc-5m-test",
        side="Up",
        action="SELL",
        order_type="taker",
        price=0.30,
        usd_amount=0.60,
        shares=2.0,
        created_at_ts=1005.0,
        latency_ms=100,
        signal_type="EARLY_EXIT",
        linked_order_id="ord-buy-1",
    )
    engine.submit_order(sell_order)
    sell_fills = engine.process_pending_orders(1005.15, books)
    assert len(sell_fills) == 1
    assert sell_fills[0].action == "SELL"

    # Open positions should be empty, settled positions should contain closed position
    assert len(engine.open_positions) == 0
    assert len(engine.settled_positions) == 1
    pos = engine.settled_positions[0]
    assert pos.status == "CLOSED_EARLY"
    assert pos.payout_usd == 0.60
    assert pytest.approx(pos.realized_pnl_usd, 0.01) == -1.20

    # Ledger check
    with ledger._get_connection() as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM paper_trades").fetchall()]
    assert len(trades) == 1
    assert trades[0]["winner"] == "EXIT_EARLY"
    assert pytest.approx(trades[0]["pnl_usd"], 0.01) == -1.20

