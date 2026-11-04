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
    """Verify reversal flip is rejected if opposite ask exceeds price ceiling (e.g. 0.92 > 0.910)."""
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
        early_exit=True,
        exit_p=0.35,
        enable_reversals=True,
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)

    books_decay = create_mock_books(up_ask=0.08, down_ask=0.92)
    strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_decay)
    assert strat.early_exit_placed

    # Opposite ask is 0.92, which exceeds price ceiling 0.910 -> REJECTED!
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1225.0, books=books_decay)
    assert sig_rev is None, "Should reject reversal if price > price_ceiling 0.910"
