from pathlib import Path
from polybot.config import RiskConfig
from polybot.risk import RiskManager


def test_risk_daily_loss_limit():
    cfg = RiskConfig(daily_loss_limit_usd=25.0)
    risk = RiskManager(cfg)

    # Initial state: allowed
    ok1, _ = risk.check_trade_allowed(proposed_stake_usd=5.0, current_window_idx=10)
    assert ok1

    # Incur $30 in losses -> exceeds $25 daily loss limit
    risk.record_trade_settled(pnl_usd=-30.0, cost_usd=30.0, current_window_idx=10)
    ok2, reason2 = risk.check_trade_allowed(proposed_stake_usd=5.0, current_window_idx=10)
    assert not ok2
    assert "daily loss limit" in reason2.lower()


def test_risk_max_trades_per_day():
    cfg = RiskConfig(max_trades_per_day=3)
    risk = RiskManager(cfg)

    risk.record_trade_placed(5.0)
    risk.record_trade_placed(5.0)
    risk.record_trade_placed(5.0)

    ok, reason = risk.check_trade_allowed(proposed_stake_usd=5.0, current_window_idx=1)
    assert not ok
    assert "max daily trades" in reason.lower()


def test_risk_consecutive_loss_cooldown():
    cfg = RiskConfig(pause_consecutive_losses=3, pause_duration_windows=3)
    risk = RiskManager(cfg)

    # 3 consecutive losses at window 10
    risk.record_trade_settled(-5.0, 5.0, current_window_idx=10)
    risk.record_trade_settled(-5.0, 5.0, current_window_idx=10)
    risk.record_trade_settled(-5.0, 5.0, current_window_idx=10)

    # Window 11 (within 3-window cooldown) -> blocked
    ok_blocked, reason_blocked = risk.check_trade_allowed(5.0, current_window_idx=11)
    assert not ok_blocked
    assert "cooldown" in reason_blocked.lower()

    # Window 14 (cooldown expired) -> allowed
    ok_resumed, _ = risk.check_trade_allowed(5.0, current_window_idx=14)
    assert ok_resumed


def test_risk_kill_switch_file(tmp_path: Path):
    stop_file = tmp_path / "STOP"
    cfg = RiskConfig(kill_switch_file=str(stop_file))
    risk = RiskManager(cfg)

    assert not risk.is_kill_switch_active()

    # Create STOP file
    stop_file.touch()
    assert risk.is_kill_switch_active()

    ok, reason = risk.check_trade_allowed(5.0, current_window_idx=1)
    assert not ok
    assert "kill switch" in reason.lower()
