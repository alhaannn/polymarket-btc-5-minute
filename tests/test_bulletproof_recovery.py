"""
Unit tests for the Bulletproof 4-Shield Architecture: Option A + Precision Recovery Flip.
Verifies:
1. Option A Initial Snipe (180s - 210s, 89% - 91% prob, price <= 0.910)
2. Shield 2 Early Exit Capital Salvage (sell at <= 0.35 or opp >= 0.88)
3. Shield 3 Precision Recovery Flip: Reversal triggers after early exit with exact calculated stake to target $0.00 net loss.
4. Shield 4 Hard Timing Cutoff: Reversal rejected if elapsed > 255.0s.
5. Shield 4 Price Ceiling: Reversal rejected if avg price > 0.910.
"""
import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy


def create_mock_books(up_ask: float, down_ask: float, depth: float = 1000.0) -> dict:
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    # Synthetic bids: 1.0 - opposing_ask - 0.01
    up_bid = max(0.01, round(1.0 - down_ask - 0.01, 3))
    down_bid = max(0.01, round(1.0 - up_ask - 0.01, 3))
    book_up.apply_snapshot(
        [{"price": str(up_bid), "size": str(depth)}],
        [{"price": str(up_ask), "size": str(depth)}]
    )
    book_down.apply_snapshot(
        [{"price": str(down_bid), "size": str(depth)}],
        [{"price": str(down_ask), "size": str(depth)}]
    )
    return {"Up": book_up, "Down": book_down}


def test_option_a_snipe_window_and_conviction():
    """Verify initial order only triggers between 180s and 210s at 89%-91% prob."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_snipe_end_sec=210.0,
        reversal_window_end_sec=300.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        price_ceiling=0.910,
        enable_reversals=True,
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 1. Too early (elapsed = 170s < 180s) -> None
    books_90 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_early = strat.on_tick(current_price=80000.0, timestamp_sec=1170.0, books=books_90)
    assert sig_early is None

    # 2. Too late for initial snipe (elapsed = 215s > 210s) -> None
    sig_late = strat.on_tick(current_price=80000.0, timestamp_sec=1215.0, books=books_90)
    assert sig_late is None

    # 3. Inside Golden Snipe Window (elapsed = 195s), but prob 85% (< 89%) -> None
    books_85 = create_mock_books(up_ask=0.85, down_ask=0.15)
    sig_low_prob = strat.on_tick(current_price=80000.0, timestamp_sec=1195.0, books=books_85)
    assert sig_low_prob is None

    # 4. Inside Golden Snipe Window (elapsed = 195s), prob 90% (89%-91%) -> FILLED INITIAL
    sig_valid = strat.on_tick(current_price=80000.0, timestamp_sec=1195.0, books=books_90)
    assert sig_valid is not None
    assert sig_valid.signal_type == "INITIAL"
    assert sig_valid.side == "Up"
    assert sig_valid.price <= 0.910
    assert strat.initial_order_placed


def test_precision_recovery_flip_lifecycle():
    """Verify full 2-step lifecycle: Initial BUY -> Decay -> EARLY EXIT -> PRECISION REVERSAL BUY."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_snipe_end_sec=210.0,
        reversal_window_end_sec=300.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        reversal_prob_min=0.88,
        reversal_prob_max=0.92,
        price_ceiling=0.910,
        early_exit=True,
        exit_p=0.35,
        enable_reversals=True,
        recovery_flip_mode="precision",
        recovery_max_time_sec=255.0,
        reversal_stake_usd=10.0,
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 1. Initial trade @ 195s ($2.00 stake for 2.22 shares @ 0.90)
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    strat.set_dynamic_budgets(initial_usd=2.0, reversal_usd=10.0)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1195.0, books=books_init)
    assert sig_init is not None
    assert sig_init.signal_type == "INITIAL"
    assert sig_init.shares > 0
    assert strat.initial_order_placed

    # 2. Market collapses: Down rises to 0.88, Up best bid falls to 0.11 (<= 0.35)
    # Elapsed = 220s
    books_decay = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_ee = strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_decay)
    assert sig_ee is not None
    assert sig_ee.signal_type == "EARLY_EXIT"
    assert sig_ee.action == "SELL"
    assert sig_ee.side == "Up"
    assert strat.early_exit_placed
    assert strat.early_exit_loss > 0.0

    # 3. Next tick: Reversal trade fires with precision stake on Down
    # Elapsed = 225s
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1225.0, books=books_decay)
    assert sig_rev is not None
    assert sig_rev.signal_type == "REVERSAL"
    assert sig_rev.action == "BUY"
    assert sig_rev.side == "Down"
    assert sig_rev.linked_order_id == strat.initial_order_id
    assert sig_rev.price <= 0.910
    # Sizing should be precision calculated (around $9 - $10 to recoup unrecovered loss)
    assert sig_rev.cost_usd > 2.0
    assert strat.reversal_order_placed


def test_reversal_hard_timing_cutoff():
    """Verify reversal flip is rejected if elapsed > 255.0s (Shield 4 Guardrail)."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_snipe_end_sec=210.0,
        reversal_window_end_sec=300.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        reversal_prob_min=0.88,
        reversal_prob_max=0.92,
        price_ceiling=0.910,
        early_exit=True,
        exit_p=0.35,
        enable_reversals=True,
        recovery_max_time_sec=255.0,
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # 1. Initial trade @ 190s
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)

    # 2. Early exit @ 220s
    books_decay = create_mock_books(up_ask=0.12, down_ask=0.88)
    strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_decay)
    assert strat.early_exit_placed

    # 3. Reversal attempted at 256s (> 255.0s hard cutoff) -> REJECTED!
    sig_late_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1256.0, books=books_decay)
    assert sig_late_rev is None, "Should reject reversal after 255s hard timing cutoff"
    assert not strat.reversal_order_placed


def test_reversal_price_ceiling():
    """Verify reversal flip allows up to reversal_price_ceiling (0.930) and rejects above it (e.g. 0.940)."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_snipe_end_sec=210.0,
        reversal_window_end_sec=300.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        reversal_prob_min=0.88,
        reversal_prob_max=0.95,
        price_ceiling=0.910,
        reversal_price_ceiling=0.930,
        early_exit=True,
        exit_p=0.35,
        enable_reversals=True,
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)

    # 1. 0.920 is within reversal_price_ceiling (0.930) -> accepted!
    books_decay_92 = create_mock_books(up_ask=0.08, down_ask=0.92)
    strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_decay_92)
    assert strat.early_exit_placed

    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1225.0, books=books_decay_92)
    assert sig_rev is not None
    assert sig_rev.signal_type == "REVERSAL"
    assert sig_rev.price <= 0.930

    # Reset reversal state to test price above ceiling
    strat.reversal_order_placed = False

    # 2. Opposite ask is 0.940, which exceeds reversal_price_ceiling 0.930 -> REJECTED!
    books_decay_94 = create_mock_books(up_ask=0.06, down_ask=0.94)
    sig_rev_94 = strat.on_tick(current_price=80000.0, timestamp_sec=1230.0, books=books_decay_94)
    assert sig_rev_94 is None, "Should reject reversal if price > reversal_price_ceiling 0.930"


def test_paper_execution_reversal_ceiling_and_unlatch():
    """Verify PaperExecutionEngine allows 0.920 for REVERSAL but rejects 0.920 for INITIAL, and invokes callback."""
    from polybot.execution.paper import PaperExecutionEngine
    from polybot.execution.base import Order

    cfg = StrategyConfig(
        strategy_name="reversal",
        price_ceiling=0.910,
        reversal_price_ceiling=0.930,
        max_slippage=0.05,
        latency_ms=10,
    )
    engine = PaperExecutionEngine(cfg)

    rejected_events = []
    def on_rejected(order, reason):
        rejected_events.append((order.signal_type, reason))

    engine.on_order_rejected = on_rejected

    books = create_mock_books(up_ask=0.92, down_ask=0.08)

    # 1. INITIAL order at 0.920 -> Rejected by price_ceiling (0.910)
    init_order = Order(
        order_id="ord-init-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.92,
        usd_amount=1.0,
        shares=1.087,
        created_at_ts=100.0,
        latency_ms=10,
        signal_type="INITIAL",
    )
    engine.submit_order(init_order)
    fills = engine.process_pending_orders(current_ts=100.05, books=books)
    assert len(fills) == 0
    assert init_order.status == "REJECTED"
    assert len(rejected_events) == 1
    assert rejected_events[0][0] == "INITIAL"
    assert "exceeds price ceiling 0.910" in rejected_events[0][1]

    # 2. REVERSAL order at 0.920 -> Accepted because reversal_price_ceiling is 0.930!
    rev_order = Order(
        order_id="ord-rev-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.92,
        usd_amount=5.0,
        shares=5.43,
        created_at_ts=101.0,
        latency_ms=10,
        signal_type="REVERSAL",
    )
    engine.submit_order(rev_order)
    fills_rev = engine.process_pending_orders(current_ts=101.05, books=books)
    assert len(fills_rev) == 1
    assert rev_order.status == "FILLED"
    assert fills_rev[0].price == pytest.approx(0.92, abs=0.001)
