import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy
from polybot.execution.fees import calculate_polymarket_5m_crypto_fee


def create_mock_books(up_ask: float, down_ask: float):
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    # Add depth so $10 buy order fills cleanly at ask
    book_up.apply_snapshot(
        [{"price": str(round(up_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(up_ask), "size": "100"}]
    )
    book_down.apply_snapshot(
        [{"price": str(round(down_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(down_ask), "size": "100"}]
    )
    return {"Up": book_up, "Down": book_down}


def test_3_trade_ladder_full_sequence_and_exact_recovery():
    """
    Tests the complete 3-Trade Precision Recovery Ladder (0.65 Triggers):
    1. Trade 1: Initial BUY Up @ 0.90 ($1.00 stake).
    2. Trade 2: Reversal 1 BUY Down @ 0.65 (recovers Trade 1 cost).
    3. Trade 3: Reversal 2 BUY Up @ 0.65 (recovers Trade 2 cost, credited by Trade 1 shares).
    4. Guard: No 4th trade allowed (max 2 reversals).
    """
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_snipe_end_sec=210.0,
        recovery_max_time_sec=275.0,
        recovery_stage2_max_time_sec=280.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        reversal_prob_min=0.65,
        reversal_prob_max=0.85,
        reversal_stage2_prob_min=0.65,
        reversal_stage2_prob_max=0.85,
        max_reversal_stages=2,
        reversal_spot_lead_usd=15.0,
        reversal_persistence_sec=1.5,
        ladder_max_window_risk_usd=10.0,
        recovery_flip_mode="precision",
        early_exit=False,
    )
    strat = ReversalStrategy(config=cfg)
    strike_K = 80000.0
    strat.reset_window(window_start=1000, strike_price_K=strike_K)

    # -------------------------------------------------------------
    # 1. TRADE 1 (INITIAL): Up at 0.90, elapsed = 190s
    # -------------------------------------------------------------
    books_t1 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_1 = strat.on_tick(current_price=80050.0, timestamp_sec=1190.0, books=books_t1)
    assert sig_1 is not None
    assert sig_1.signal_type == "INITIAL"
    assert sig_1.side == "Up"
    assert pytest.approx(sig_1.cost_usd, rel=1e-2) == 1.00
    assert strat.reversal_stage == 0
    assert strat.shares_held["Up"] > 0
    t1_shares = strat.shares_held["Up"]

    # -------------------------------------------------------------
    # 2. TRADE 2 (REVERSAL 1): Market flips to Down at 0.65, elapsed = 230s
    # Spot price drops to 79980.0 ($20 lead below strike_K)
    # -------------------------------------------------------------
    books_t2 = create_mock_books(up_ask=0.35, down_ask=0.65)
    
    # Tick 1: Lead established, but persistence (1.5s) not yet met
    sig_2_premature = strat.on_tick(current_price=79980.0, timestamp_sec=1230.0, books=books_t2)
    assert sig_2_premature is None  # Waiting for 1.5s persistence

    # Tick 2: 1.6s later (persistence met)
    sig_2 = strat.on_tick(current_price=79980.0, timestamp_sec=1231.6, books=books_t2)
    assert sig_2 is not None
    assert sig_2.signal_type == "REVERSAL_1"
    assert sig_2.side == "Down"
    # Sizing for leg 2 should be ~$1.97 to $2.05 to recover ~$1.007 spent on T1
    assert 1.90 <= sig_2.cost_usd <= 2.15
    assert strat.reversal_stage == 1
    assert strat.shares_held["Down"] > 0
    t2_shares = strat.shares_held["Down"]

    # If Down wins here:
    # Payout = t2_shares * $1.00
    # Net PnL = Payout - (sig_1.cost_usd + sig_1.fee_usd + sig_2.cost_usd + sig_2.fee_usd)
    t2_payout = t2_shares * 1.0
    t2_total_spent = sig_1.cost_usd + sig_1.fee_usd + sig_2.cost_usd + sig_2.fee_usd
    net_pnl_if_down_wins = t2_payout - t2_total_spent
    assert -0.05 <= net_pnl_if_down_wins <= 0.10, f"Expected ~breakeven on T2 win, got {net_pnl_if_down_wins}"

    # -------------------------------------------------------------
    # 3. TRADE 3 (REVERSAL 2): Market swings back to Up at 0.65, elapsed = 260s
    # Spot price rises to 80025.0 ($25 lead above strike_K)
    # -------------------------------------------------------------
    books_t3 = create_mock_books(up_ask=0.65, down_ask=0.35)

    # Tick 1: Lead established, persistence waiting
    assert strat.on_tick(current_price=80025.0, timestamp_sec=1260.0, books=books_t3) is None

    # Tick 2: 1.6s later
    sig_3 = strat.on_tick(current_price=80025.0, timestamp_sec=1261.6, books=books_t3)
    assert sig_3 is not None
    assert sig_3.signal_type == "REVERSAL_2"
    assert sig_3.side == "Up"
    # Sizing for leg 3 is credited by T1 shares!
    # Expected base stake ~$3.70 to $4.00
    assert 3.50 <= sig_3.cost_usd <= 4.10
    assert strat.reversal_stage == 2

    # Verify cumulative cash spent fits inside budget ($6.85 typical, <= $7.50)
    total_spent = strat.total_spent_usd
    assert total_spent <= 7.50

    # If Up wins here:
    # Total Up shares held = t1_shares + sig_3.shares
    total_up_shares = strat.shares_held["Up"]
    total_window_fees = sig_1.fee_usd + sig_2.fee_usd + sig_3.fee_usd
    total_window_cost = strat.total_spent_usd + total_window_fees
    net_pnl_if_up_wins = total_up_shares * 1.0 - total_window_cost
    assert -0.05 <= net_pnl_if_up_wins <= 0.15, f"Expected ~breakeven on T3 win, got {net_pnl_if_up_wins}"

    # -------------------------------------------------------------
    # 4. STAGE CUTOFF: Max 2 reversals reached -> NO TRADE 4
    # -------------------------------------------------------------
    # Market flips again to Down at 0.65
    sig_4 = strat.on_tick(current_price=79970.0, timestamp_sec=1270.0, books=books_t2)
    assert sig_4 is None, "Should not trigger a 4th trade after 2 reversals!"


def test_spot_lead_filter_blocks_wicks():
    """Verify that a 1-second price wick without spot lead does not trigger reversal."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        reversal_prob_min=0.65,
        reversal_prob_max=0.85,
        reversal_spot_lead_usd=15.0,
        reversal_persistence_sec=1.5,
        recovery_flip_mode="precision",
    )
    strat = ReversalStrategy(config=cfg)
    strike_K = 80000.0
    strat.reset_window(window_start=1000, strike_price_K=strike_K)

    # Initial Up trade
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_init = strat.on_tick(current_price=80020.0, timestamp_sec=1190.0, books=books_init)
    assert sig_init is not None

    # Down books show 0.65, but spot price is at 80005.0 (still ABOVE strike 80000.0!)
    books_rev = create_mock_books(up_ask=0.35, down_ask=0.65)
    sig_wick = strat.on_tick(current_price=80005.0, timestamp_sec=1220.0, books=books_rev)
    assert sig_wick is None, "Reversal should be blocked when spot price does not lead across strike!"


def test_window_risk_hard_cap_enforcement():
    """Verify that cumulative window spend cannot exceed ladder_max_window_risk_usd."""
    cfg = StrategyConfig(
        prime_hours_only=False,
        reversal_window_start_sec=180.0,
        reversal_prob_min=0.65,
        reversal_prob_max=0.85,
        ladder_max_window_risk_usd=3.0,  # Strict cap of $3.00
        recovery_flip_mode="precision",
    )
    strat = ReversalStrategy(config=cfg)
    strat.reset_window(window_start=1000)

    # Initial order ($1.00)
    books_init = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_1 = strat.on_tick(current_price=80000.0, timestamp_sec=1190.0, books=books_init)
    assert sig_1 is not None

    # Reversal attempts to spend ~$2.00. Total would be ~$3.00.
    # If we artificially set total_spent_usd to $2.50, remaining risk is $0.50 (< $1.00 floor)
    strat.total_spent_usd = 2.50
    books_rev = create_mock_books(up_ask=0.35, down_ask=0.65)
    sig_blocked = strat.on_tick(current_price=80000.0, timestamp_sec=1220.0, books=books_rev)
    assert sig_blocked is None, "Should reject reversal when remaining risk cap is < $1.00 minimum floor"
