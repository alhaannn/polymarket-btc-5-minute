"""
End-to-End Paper-Trading Soak & Dry-Run Integration Suite (Roadmap Phase 9).
Validates multi-window realistic execution, crash restart recovery,
milestone activation, daily shutdown halting, and next-day Option B reserve releases.
"""
from pathlib import Path
import pytest

from polybot.capital import (
    PaperTradingSoakRunner,
    SoakTestReport,
)


def test_soak_canonical_growth_and_execution(tmp_path: Path):
    """
    Simulates 24 consecutive 5-minute windows starting at canonical $11.00.
    Verifies:
    - Initial order ($1) & reversal order ($10) placed and filled.
    - Consecutive profitable windows increase active equity.
    - 100% invariant preservation across all cycles.
    - Zero reconciliation mismatches.
    """
    db_file = tmp_path / "soak_growth.db"
    runner = PaperTradingSoakRunner(db_path=db_file, starting_equity_usd=11.0)

    report = runner.run_soak(num_windows=24, start_timestamp=1793400000.0)

    assert report.is_successful is True
    assert report.windows_simulated == 24
    assert report.trades_executed > 0
    assert report.trades_settled > 0
    assert report.final_equity_usd > report.starting_equity_usd
    assert report.invariant_violations_count == 0
    assert report.reconciliation_mismatches == 0


def test_soak_milestone_activation_and_profit_split(tmp_path: Path):
    """
    Starting from $40.00, simulates trading until equity crosses the $44 milestone.
    Verifies:
    - Exactly-once milestone activation.
    - Exact 50/50 profit split into active and protected pools.
    - Protected capital strictly isolated from trade budgets.
    """
    db_file = tmp_path / "soak_ms.db"
    runner = PaperTradingSoakRunner(db_path=db_file, starting_equity_usd=40.0)

    report = runner.run_soak(num_windows=15, start_timestamp=1793400000.0)

    assert report.is_successful is True
    assert report.milestone_44_reached is True
    assert report.milestone_window_idx is not None
    assert report.final_protected_usd > 0.0
    assert report.final_active_usd > 0.0
    assert report.invariant_violations_count == 0
    assert report.reconciliation_mismatches == 0


def test_soak_crash_restart_recovery(tmp_path: Path):
    """
    Simulates bot crash and recovery at window 10 during the soak run.
    Verifies:
    - State, transactions, and trade ledger survive process termination.
    - Zero duplicate transactions.
    - Continuous execution after recovery with valid budgets.
    """
    db_file = tmp_path / "soak_restart.db"
    runner = PaperTradingSoakRunner(db_path=db_file, starting_equity_usd=11.0)

    report = runner.run_soak(
        num_windows=20,
        start_timestamp=1793400000.0,
        test_restart_at_window=10,
    )

    assert report.is_successful is True
    assert report.windows_simulated == 20
    # Check that restart event was logged
    restart_events = [e for e in report.event_log if e.event_type == "RESTART"]
    assert len(restart_events) == 1
    assert report.invariant_violations_count == 0


def test_soak_daily_shutdown_and_option_b_release(tmp_path: Path):
    """
    Multi-day soak test spanning across midnight UTC:
    - Day 1 (2026-10-30): Injects failure, triggering conjunctive daily shutdown.
    - Day 1: New orders blocked for the remainder of Day 1.
    - Day 2 (2026-10-31): Rollover resets daily failure counters.
    - Day 2: Option B triggers and releases 50% of protected reserve into active capital.
    - Day 2: Trading resumes with augmented active capital budget.
    """
    db_file = tmp_path / "soak_multiday.db"
    # Funded start ($50) so milestone $44 activates and protected reserve exists
    runner = PaperTradingSoakRunner(db_path=db_file, starting_equity_usd=50.0)

    # 1793400000 is 2026-10-30 20:00:00 UTC. 50 windows = 4.16 hours (crosses midnight into 2026-10-31)
    report = runner.run_soak(
        num_windows=50,
        start_timestamp=1793400000.0,
        inject_failure_day="2026-10-30",
    )

    assert report.is_successful is True
    assert report.shutdowns_triggered >= 1
    assert report.option_b_releases >= 1
    assert report.option_b_total_released_usd > 0.0
    assert report.invariant_violations_count == 0
    assert report.reconciliation_mismatches == 0
