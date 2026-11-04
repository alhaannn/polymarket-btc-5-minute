"""
Unit tests for IST timezone formatting and database reset functionality.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from polybot.capital import CapitalManager
from polybot.execution.ledger import TradeLedger
from polybot.reset import reset_paper_database
from polybot.tz import IST_TZ, ist_datetime_str, ist_time_str, ist_today_str, now_ist, to_ist


def test_ist_timezone_functions():
    dt = now_ist()
    assert dt.tzinfo == IST_TZ
    # Offset is 5 hours 30 minutes = 19800 seconds
    assert dt.utcoffset().total_seconds() == 19800

    # Specific timestamp: 2026-11-01 00:00:00 UTC (1793491200) -> 05:30:00 IST
    t_fixed = 1793491200.0
    ist_str = ist_time_str(t_fixed)
    assert ist_str == "05:30:00"

    day_str = ist_today_str(t_fixed)
    assert day_str == "2026-11-01"

    dt_str = ist_datetime_str(t_fixed)
    assert "2026-11-01 05:30:00 IST" in dt_str


def test_reset_paper_database(tmp_path: Path):
    db_file = tmp_path / "test_paper_ledger.db"

    # Setup database with simulated trades and capital
    ledger = TradeLedger(db_file)
    ledger.record_trade(
        order_id="test-1",
        slug="test-slug-1",
        side="Up",
        action="BUY",
        order_type="taker",
        signal_type="INITIAL",
        stake_usd=1.0,
        shares=1.1,
        avg_price=0.9,
        fee_usd=0.01,
        exec_ts=time.time(),
    )
    ledger.settle_order("test-1", "Up", 1.1, 0.09, time.time())

    cap_mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    assert cap_mgr.state.total_equity_cents == 1100

    # Verify trade was recorded
    with sqlite3.connect(str(db_file)) as conn:
        assert conn.execute("SELECT count(*) FROM paper_trades").fetchone()[0] == 1

    # Execute reset
    success = reset_paper_database(db_path=db_file, starting_equity_usd=11.0, backup=True)
    assert success is True

    # Check that backup exists
    backups = list(tmp_path.glob("test_paper_ledger.db.bak.*"))
    assert len(backups) == 1

    # Reload capital manager & check state
    fresh_cap = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    assert fresh_cap.state.total_equity_cents == 1100
    assert fresh_cap.state.active_capital_cents == 1100
    assert fresh_cap.state.protected_capital_cents == 0
    assert fresh_cap.state.realized_profit_cents == 0
    assert fresh_cap.state.realized_loss_cents == 0
    assert fresh_cap.state.daily_shutdown_status is False
    assert fresh_cap.state.milestone_44_activated is False

    # Verify trades table is completely cleared (0 trades)
    fresh_ledger = TradeLedger(db_file)
    assert len(fresh_ledger.get_recent_trades(limit=10)) == 0

    # Verify performance summary reflects fresh state with IST timezone
    perf = fresh_ledger.get_performance_summary()
    assert perf["timezone"] == "IST"
    assert perf["lifetime"]["total_trades"] == 0
    assert perf["lifetime"]["net_pnl_usd"] == 0.0


def test_compute_schedule_state():
    import datetime
    from polybot.tz import compute_schedule_state

    # 1. Test Resting at 13:00:00 IST (30 minutes before 13:30 start)
    dt_resting = datetime.datetime(2026, 11, 4, 13, 0, 0, tzinfo=IST_TZ)
    res_resting = compute_schedule_state(prime_hours_only=True, ts=dt_resting.timestamp())
    assert res_resting["status"] == "RESTING"
    assert res_resting["is_working"] is False
    assert res_resting["time_left_to_start_sec"] == 1800.0
    assert res_resting["time_left_to_start_formatted"] == "30m 00s"

    # 2. Test Working at 14:00:00 IST (30 minutes after 13:30 start)
    dt_working = datetime.datetime(2026, 11, 4, 14, 0, 0, tzinfo=IST_TZ)
    res_working = compute_schedule_state(prime_hours_only=True, ts=dt_working.timestamp())
    assert res_working["status"] == "WORKING"
    assert res_working["is_working"] is True
    assert res_working["working_elapsed_sec"] == 1800.0
    assert res_working["working_elapsed_formatted"] == "30m 00s"
    assert res_working["working_remaining_sec"] == 39600.0  # 11 hours left to 01:00

    # 3. Test Working at 00:30:00 IST (past midnight, 30m before 01:00 close)
    dt_midnight = datetime.datetime(2026, 11, 5, 0, 30, 0, tzinfo=IST_TZ)
    res_midnight = compute_schedule_state(prime_hours_only=True, ts=dt_midnight.timestamp())
    assert res_midnight["status"] == "WORKING"
    assert res_midnight["is_working"] is True
    assert res_midnight["working_remaining_sec"] == 1800.0  # 30m to 01:00
    assert res_midnight["working_elapsed_sec"] == 39600.0  # 11 hours elapsed since 13:30 yesterday

    # 4. Test Daily Shutdown override during prime hours
    res_shutdown = compute_schedule_state(
        prime_hours_only=True,
        shutdown_active=True,
        shutdown_until_ts=dt_working.timestamp() + 3600.0,
        ts=dt_working.timestamp(),
    )
    assert res_shutdown["status"] == "RESTING"
    assert res_shutdown["is_working"] is False
    assert res_shutdown["reason"] == "Daily Shutdown / Risk Cooldown Active"
    assert res_shutdown["time_left_to_start_sec"] == 3600.0

    # 5. Test Session PnL Calculation
    session_start_ts = datetime.datetime(2026, 11, 4, 13, 30, 0, tzinfo=IST_TZ).timestamp()
    sample_trades = [
        {"order_id": "old", "status": "SETTLED", "pnl_usd": 10.0, "exec_ts": session_start_ts - 500},
        {"order_id": "t1", "status": "SETTLED", "pnl_usd": 0.42, "exec_ts": session_start_ts + 100},
        {"order_id": "t2", "status": "SETTLED", "pnl_usd": -0.20, "exec_ts": session_start_ts + 200},
    ]
    res_pnl = compute_schedule_state(
        prime_hours_only=True,
        trades=sample_trades,
        ts=dt_working.timestamp(),
    )
    assert res_pnl["session_trades_count"] == 2
    assert res_pnl["session_wins"] == 1
    assert res_pnl["session_losses"] == 1
    assert res_pnl["session_pnl_usd"] == 0.22
