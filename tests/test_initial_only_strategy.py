import datetime
import pytest
from pathlib import Path
from polybot.capital.manager import CapitalManager
from polybot.config import RiskConfig, StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.risk import RiskManager
from polybot.strategy import ReversalStrategy
from polybot.tz import IST_TZ, session_day_key


def create_mock_books(up_ask: float, down_ask: float):
    book_up = TokenOrderBook("tok-up")
    book_down = TokenOrderBook("tok-down")
    book_up.apply_snapshot(
        [{"price": str(round(up_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(up_ask), "size": "100"}]
    )
    book_down.apply_snapshot(
        [{"price": str(round(down_ask - 0.01, 3)), "size": "100"}],
        [{"price": str(down_ask), "size": "100"}]
    )
    return {"Up": book_up, "Down": book_down}


def test_initial_only_entry_and_zero_reversals():
    """Initial-Only (Maximum Safety): Takes initial trade at 90c, zero reversals taken even on severe adverse price move."""
    cfg = StrategyConfig(
        strategy_name="reversal",
        initial_stake_usd=1.00,
        reversal_stake_usd=0.00,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        early_exit=False,
        enable_reversals=False,
        max_reversal_stages=0,
        prime_hours_only=False,
        entry_start_sec=180.0,
        entry_end_sec=210.0,
    )
    strat = ReversalStrategy(cfg)
    strat.reset_window(window_start=1000, strike_price_K=65000.0)

    # 1. At 100s, no entry
    books_100 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1100.0,
        books=books_100,
    )
    assert sig is None

    # 2. At 190s (inside 180-210s window), Up is 0.90 -> Initial entry triggers
    books_190 = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig = strat.on_tick(
        current_price=65150.0,
        timestamp_sec=1190.0,
        books=books_190,
    )
    assert sig is not None
    assert sig.signal_type == "INITIAL"
    assert sig.side == "Up"
    assert pytest.approx(sig.price, rel=1e-3) == 0.90
    assert sig.cost_usd == pytest.approx(1.00, abs=0.01)

    # 3. Market reverses severely: Up crashes to 0.10, Down explodes to 0.90 at 240s
    books_rev = create_mock_books(up_ask=0.10, down_ask=0.90)
    for t in [1220.0, 1240.0, 1260.0, 1280.0]:
        sig_rev = strat.on_tick(
            current_price=64800.0,
            timestamp_sec=t,
            books=books_rev,
        )
        # ZERO reversals should be placed!
        assert sig_rev is None, f"Expected None but got reversal signal {sig_rev} at {t}"


def test_initial_only_5_dollar_stake_and_prime_hours():
    """Initial-Only with $5.00 stake and restricted prime hours only."""
    cfg = StrategyConfig(
        strategy_name="reversal",
        initial_stake_usd=5.00,
        reversal_stake_usd=0.00,
        initial_prob_min=0.89,
        initial_prob_max=0.91,
        early_exit=False,
        enable_reversals=False,
        max_reversal_stages=0,
        prime_hours_only=True,
        entry_start_sec=180.0,
        entry_end_sec=210.0,
    )
    strat = ReversalStrategy(cfg)
    strat.set_dynamic_budgets(initial_usd=5.0, reversal_usd=0.0)

    # 1. Non-prime hour: 06:00 IST (timestamp 1795134600.0)
    strat.reset_window(window_start=1795134600, strike_price_K=65000.0)
    books_np = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_np = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1795134600.0 + 190.0,
        books=books_np,
    )
    # Blocked by restricted prime hours
    assert sig_np is None

    # 2. Prime hour: 15:00 IST (timestamp 1795167000)
    strat.reset_window(window_start=1795167000, strike_price_K=65000.0)
    strat.set_dynamic_budgets(initial_usd=5.0, reversal_usd=0.0)
    books_prime = create_mock_books(up_ask=0.90, down_ask=0.10)
    sig_prime = strat.on_tick(
        current_price=65100.0,
        timestamp_sec=1795167000.0 + 190.0,
        books=books_prime,
    )
    assert sig_prime is not None
    assert sig_prime.signal_type == "INITIAL"
    assert pytest.approx(sig_prime.cost_usd, abs=0.05) == 5.00


def test_v2_price_cap_090_and_counterpart_spread_filter():
    """v2 rejects 0.91 fills and rejects wide counterpart_prob (> 0.13), while allowing clean 0.89-0.90 / cp<=0.13."""
    cfg = StrategyConfig(
        strategy_name="reversal",
        initial_stake_usd=5.00,
        reversal_stake_usd=0.00,
        initial_prob_min=0.89,
        initial_prob_max=0.90,
        price_ceiling=0.900,
        max_initial_counterpart_prob=0.13,
        early_exit=False,
        enable_reversals=False,
        max_reversal_stages=0,
        prime_hours_only=False,
        entry_start_sec=180.0,
        entry_end_sec=210.0,
    )
    strat = ReversalStrategy(cfg)
    strat.set_dynamic_budgets(initial_usd=5.0, reversal_usd=0.0)
    strat.reset_window(window_start=1000, strike_price_K=65000.0)

    # 1. 0.91 rejected by initial_prob_max = 0.90
    books_91 = create_mock_books(up_ask=0.91, down_ask=0.10)
    assert strat.on_tick(65100.0, 1190.0, books_91) is None

    # 2. 0.89 with wide counterpart_prob=0.14 rejected by max_initial_counterpart_prob=0.13
    books_wide = create_mock_books(up_ask=0.89, down_ask=0.14)
    assert strat.on_tick(65100.0, 1191.0, books_wide) is None

    # 3. 0.89 with clean counterpart_prob=0.12 fills normally
    books_clean = create_mock_books(up_ask=0.89, down_ask=0.12)
    sig = strat.on_tick(65100.0, 1192.0, books_clean)
    assert sig is not None
    assert sig.side == "Up"
    assert sig.counterpart_prob == pytest.approx(0.12, abs=1e-3)


def test_session_aligned_rollover_trailing_lock_and_max_loss():
    """RiskManager session keys stay intact across 00:00 IST midnight, enforce -$10 max loss and Trail(+$6/$5), and reset at 13:30 IST."""
    t_evening = datetime.datetime(2026, 11, 26, 22, 0, 0, tzinfo=IST_TZ).timestamp()
    t_after_midnight = datetime.datetime(2026, 11, 27, 0, 30, 0, tzinfo=IST_TZ).timestamp()
    t_next_session = datetime.datetime(2026, 11, 27, 13, 35, 0, tzinfo=IST_TZ).timestamp()

    assert session_day_key(t_evening, prime_hours_only=True) == "2026-11-26_1330"
    assert session_day_key(t_after_midnight, prime_hours_only=True) == "2026-11-26_1330"
    assert session_day_key(t_next_session, prime_hours_only=True) == "2026-11-27_1330"

    rc = RiskConfig(
        disable_daily_shutdown=True,
        daily_loss_limit_usd=0.0,
        session_aligned_reset=True,
        prime_hours_only=True,
        session_max_loss_usd=10.0,
        trailing_arm_usd=6.0,
        trailing_drop_usd=5.0,
        pause_consecutive_losses=100,
    )
    risk = RiskManager(rc)
    risk._check_day_rollover(t_evening)

    # Simulate reaching +$6.50 peak then losing -$5.005 -> PnL +$1.495 (drop=$5.005 >= $5.00) -> Trailing lock triggers!
    for i in range(11):
        risk.record_trade_placed(5.0, current_ts=t_evening + i * 10)
        risk.record_trade_settled(0.59, 5.0, current_window_idx=i, current_ts=t_evening + i * 10 + 1)
    assert risk.session_peak_pnl == pytest.approx(6.49, abs=0.05)
    assert not risk.session_shutdown_active

    risk.record_trade_placed(5.0, current_ts=t_evening + 500)
    risk.record_trade_settled(-5.005, 5.0, current_window_idx=12, current_ts=t_evening + 505)
    assert risk.session_shutdown_active is True
    assert "Trailing profit lock" in risk.session_shutdown_reason

    # Must REMAIN locked after 00:00 IST midnight (00:30 IST is same session!)
    ok_midnight, reason_m = risk.check_trade_allowed(5.0, 20, current_ts=t_after_midnight)
    assert ok_midnight is False
    assert "Trailing profit lock" in reason_m

    # Must cleanly RESET at 13:30 IST next day!
    ok_next, _ = risk.check_trade_allowed(5.0, 30, current_ts=t_next_session)
    assert ok_next is True
    assert not risk.session_shutdown_active
    assert risk.daily_pnl == 0.0

    # Test -$10.00 max loss and ledger hydration across restart
    t_s2 = t_next_session + 60
    trades = [
        {"exec_ts": t_s2, "settled_at_ts": t_s2 + 100, "status": "SETTLED", "pnl_usd": -5.005, "stake_usd": 5.0},
        {"exec_ts": t_s2 + 300, "settled_at_ts": t_s2 + 400, "status": "SETTLED", "pnl_usd": -5.005, "stake_usd": 5.0},
    ]
    risk2 = RiskManager(rc)
    risk2.hydrate_session_trades(trades, current_ts=t_s2 + 500)
    assert risk2.session_shutdown_active is True
    assert "Session max loss" in risk2.session_shutdown_reason


def test_fixed_mode_disables_milestone_split(tmp_path: Path):
    """CapitalManager with enable_milestone_split=False keeps 100% of equity in active capital above $44."""
    db_file = tmp_path / "cap_fixed.db"
    cm = CapitalManager(
        db_path=db_file,
        starting_equity_usd=50.0,
        sizing_mode="fixed",
        disable_daily_shutdown=True,
        enable_milestone_split=False,
    )
    cm.process_settlement("s-1", net_pnl_usd=0.61, fee_usd=0.005, trading_day="2026-11-26", signal_type="INITIAL")
    assert not cm.state.milestone_44_activated
    assert cm.state.protected_capital_cents == 0
    assert cm.state.active_capital_cents == 5061
