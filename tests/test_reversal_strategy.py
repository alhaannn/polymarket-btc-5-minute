import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy


def create_mock_books(up_ask: float, down_ask: float):
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    # Apply book with liquidity
    book_up.apply_snapshot([{"price": str(round(up_ask - 0.01, 3)), "size": "100"}], [{"price": str(up_ask), "size": "100"}])
    book_down.apply_snapshot([{"price": str(round(down_ask - 0.01, 3)), "size": "100"}], [{"price": str(down_ask), "size": "100"}])
    return {"Up": book_up, "Down": book_down}


def test_waiting_phase_no_trade():
    """Rule 4 & 5: No trade in first 180 seconds even if in 89-92% range."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    books = create_mock_books(up_ask=0.90, down_ask=0.10)
    # At elapsed 179.9s (< 180s)
    sig = strat.on_tick(current_price=80000.0, timestamp_sec=1179.9, books=books)
    assert sig is None
    assert not strat.initial_order_placed


def test_initial_entry_boundaries_and_amount():
    """Rule 5: Initial order at 89%-92% inclusive with $1 stake."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    # 1. Below 89% (0.889) -> No trade
    b_low = create_mock_books(up_ask=0.889, down_ask=0.111)
    sig_low = strat.on_tick(current_price=80000.0, timestamp_sec=1185.0, books=b_low)
    assert sig_low is None

    # 2. Above 92% (0.921) -> No trade
    b_high = create_mock_books(up_ask=0.921, down_ask=0.079)
    sig_high = strat.on_tick(current_price=80000.0, timestamp_sec=1185.0, books=b_high)
    assert sig_high is None

    # 3. Exactly 89% (0.890) -> Initial trade triggered on Up
    b_89 = create_mock_books(up_ask=0.890, down_ask=0.110)
    sig_89 = strat.on_tick(current_price=80000.0, timestamp_sec=1185.0, books=b_89)
    assert sig_89 is not None
    assert sig_89.side == "Up"
    assert sig_89.signal_type == "INITIAL"
    assert pytest.approx(sig_89.cost_usd, rel=1e-3) == 1.0
    assert strat.initial_order_placed
    assert strat.initial_side == "Up"

    # 4. Same window next tick -> No duplicate initial order
    sig_dup = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=b_89)
    assert sig_dup is None


def test_initial_entry_on_down():
    """Initial trade can trigger on Down if Down is 89-92%."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    b_down = create_mock_books(up_ask=0.09, down_ask=0.91)
    sig = strat.on_tick(current_price=80000.0, timestamp_sec=1185.0, books=b_down)
    assert sig is not None
    assert sig.side == "Down"
    assert sig.signal_type == "INITIAL"
    assert pytest.approx(sig.cost_usd, rel=1e-3) == 1.0
    assert strat.initial_side == "Down"


def test_reversal_entry_and_multiplier():
    """Rule 6: After initial trade, monitor opposite outcome for 85-90% inclusive, stake = 10x ($10)."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    # Initial order on Up at elapsed=185s
    b_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_init = strat.on_tick(current_price=80000.0, timestamp_sec=1185.0, books=b_init)
    assert sig_init is not None
    assert sig_init.side == "Up"
    initial_order_id = strat.initial_order_id

    # Market shifts, opposite outcome (Down) enters 84% (< 85%) -> No reversal
    b_opp_low = create_mock_books(up_ask=0.16, down_ask=0.84)
    sig_rev_low = strat.on_tick(current_price=80000.0, timestamp_sec=1200.0, books=b_opp_low)
    assert sig_rev_low is None

    # Opposite outcome enters 91% (> 90%) -> No reversal
    b_opp_high = create_mock_books(up_ask=0.09, down_ask=0.91)
    sig_rev_high = strat.on_tick(current_price=80000.0, timestamp_sec=1205.0, books=b_opp_high)
    assert sig_rev_high is None

    # Opposite outcome enters 88% (inside [0.85, 0.90]) -> REVERSAL TRIGGERED!
    b_opp_in = create_mock_books(up_ask=0.12, down_ask=0.88)
    sig_rev = strat.on_tick(current_price=80000.0, timestamp_sec=1210.0, books=b_opp_in)
    assert sig_rev is not None
    assert sig_rev.side == "Down"
    assert sig_rev.signal_type == "REVERSAL"
    assert pytest.approx(sig_rev.cost_usd, rel=1e-3) == 10.0  # 10x multiplier ($10)
    assert sig_rev.linked_order_id == initial_order_id
    assert strat.reversal_order_placed

    # Next tick -> No duplicate reversal
    sig_rev_dup = strat.on_tick(current_price=80000.0, timestamp_sec=1215.0, books=b_opp_in)
    assert sig_rev_dup is None


def test_market_close_boundary():
    """Rule 3: At or after 300 seconds, no new orders are allowed."""
    strat = ReversalStrategy()
    strat.reset_window(window_start=1000)

    books = create_mock_books(up_ask=0.90, down_ask=0.10)
    # At elapsed 300.0s
    sig = strat.on_tick(current_price=80000.0, timestamp_sec=1300.0, books=books)
    assert sig is None

    # At elapsed 305.0s
    sig_late = strat.on_tick(current_price=80000.0, timestamp_sec=1305.0, books=books)
    assert sig_late is None
