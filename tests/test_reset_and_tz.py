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
