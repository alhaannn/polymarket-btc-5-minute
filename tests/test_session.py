import pytest
from polybot.session import MarketPhase, MarketSessionManager


def test_session_phases_and_boundaries():
    mgr = MarketSessionManager(window_duration_sec=300.0, waiting_period_sec=180.0)
    base_start = 1700000000  # aligned to 300s window: 1700000000 % 300 = 200 -> let's use exact window:
    # 1700000100 // 300 * 300 = 1700000100 - (1700000100 % 300)
    window_start = (1700000000 // 300) * 300

    # 1. At start (t=0)
    s0 = mgr.get_state_for_time(window_start + 0.0)
    assert s0.phase == MarketPhase.WAITING
    assert not s0.is_trading_allowed
    assert s0.elapsed_sec == 0.0
    assert s0.remaining_sec == 300.0

    # 2. At 179.999s (just before 180s)
    s_pre = mgr.get_state_for_time(window_start + 179.999)
    assert s_pre.phase == MarketPhase.WAITING
    assert not s_pre.is_trading_allowed
    assert s_pre.elapsed_sec == 179.999
    assert s_pre.remaining_sec == 120.001

    # 3. Exactly at 180.000s (window opens)
    s_open = mgr.get_state_for_time(window_start + 180.0)
    assert s_open.phase == MarketPhase.ACTIVE
    assert s_open.is_trading_allowed
    assert s_open.elapsed_sec == 180.0
    assert s_open.remaining_sec == 120.0

    # 4. In trading window at 240s
    s_mid = mgr.get_state_for_time(window_start + 240.0)
    assert s_mid.phase == MarketPhase.ACTIVE
    assert s_mid.is_trading_allowed

    # 5. Just before close at 299.999s
    s_pre_close = mgr.get_state_for_time(window_start + 299.999)
    assert s_pre_close.phase == MarketPhase.ACTIVE
    assert s_pre_close.is_trading_allowed

    # 6. At 300.000s for this specific window (window closed)
    s_close = mgr.get_state_for_time(window_start + 300.0, target_window_start=window_start)
    assert s_close.phase == MarketPhase.CLOSED
    assert not s_close.is_trading_allowed
    assert s_close.remaining_sec == 0.0

    # 7. Without target_window_start, window_start + 300.0 rolls over into new window t=0
    s_rollover = mgr.get_state_for_time(window_start + 300.0)
    assert s_rollover.phase == MarketPhase.WAITING
    assert not s_rollover.is_trading_allowed
    assert s_rollover.elapsed_sec == 0.0


def test_session_sync_live():
    mgr = MarketSessionManager(window_duration_sec=300.0, waiting_period_sec=180.0)
    state = mgr.sync()
    assert state.window_start_ts <= state.window_end_ts
    assert state.remaining_sec >= 0.0
    assert state.elapsed_sec >= 0.0
    assert state.phase in (MarketPhase.WAITING, MarketPhase.ACTIVE, MarketPhase.CLOSED)
