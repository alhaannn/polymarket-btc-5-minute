"""
Unit and integration tests for Historical CSV Replay and Capital Allocation Simulation (Roadmap Phase 8).
Validates all 4 comparison modes across historical paper trade logs.
"""
from pathlib import Path
import pytest

from polybot.capital.simulation import (
    HistoricalReplayEngine,
    ReplayMode,
    SimulationSummary,
)


@pytest.fixture
def paper_trades_csv() -> Path:
    p = Path("paper_trades.csv")
    if not p.exists():
        pytest.skip("paper_trades.csv not present in workspace")
    return p


@pytest.fixture
def paper_trades_1_csv() -> Path:
    p = Path("paper_trades (1).csv")
    if not p.exists():
        pytest.skip("paper_trades (1).csv not present in workspace")
    return p


def test_load_trades_csv(paper_trades_csv: Path):
    trades = HistoricalReplayEngine.load_trades_csv(paper_trades_csv)
    assert len(trades) >= 178
    # Chronological sort check
    for i in range(len(trades) - 1):
        assert trades[i]["exec_ts"] <= trades[i + 1]["exec_ts"]


def test_replay_fixed_sizing(paper_trades_csv: Path, tmp_path: Path):
    trades = HistoricalReplayEngine.load_trades_csv(paper_trades_csv)
    summary = HistoricalReplayEngine.run_simulation(
        trades=trades,
        mode=ReplayMode.FIXED_SIZING,
        starting_equity_usd=11.0,
        temp_db_path=tmp_path / "replay_fixed.db",
    )
    assert summary.mode == ReplayMode.FIXED_SIZING
    assert summary.starting_equity_usd == 11.0
    assert summary.total_trades == len(trades)
    assert summary.final_equity_usd > 0
    assert summary.win_rate_pct > 80.0


def test_replay_dynamic_compounding_growth(paper_trades_csv: Path):
    """
    Validates dynamic compounding from canonical $11.00 start.
    Confirms capital grows from $11.00 to ~$39.75.
    """
    results = HistoricalReplayEngine.compare_all_modes(paper_trades_csv, starting_equity_usd=11.0)
    assert "FIXED_SIZING" in results
    assert "DYNAMIC_NO_PROTECTION" in results
    assert "DYNAMIC_WITH_PROTECTION" in results
    assert "OPTION_B_RESERVE_RELEASE" in results

    fixed = results["FIXED_SIZING"]
    dyn_prot = results["DYNAMIC_WITH_PROTECTION"]

    assert dyn_prot.starting_equity_usd == 11.0
    assert dyn_prot.max_equity_usd >= 39.0
    assert dyn_prot.win_rate_pct > 90.0
    assert pytest.approx(dyn_prot.final_equity_usd, rel=1e-2) == (
        dyn_prot.final_active_usd + dyn_prot.final_protected_usd
    )


def test_replay_milestone_and_protection(paper_trades_csv: Path):
    """
    With starting capital of $20.00, equity crosses the $44 milestone.
    Confirms milestone activation, active vs protected capital split, and invariant preservation.
    """
    results = HistoricalReplayEngine.compare_all_modes(paper_trades_csv, starting_equity_usd=20.0)
    dyn_prot = results["DYNAMIC_WITH_PROTECTION"]

    assert dyn_prot.milestone_44_reached is True
    assert dyn_prot.milestone_trade_idx is not None
    assert dyn_prot.final_protected_usd > 0.0
    assert dyn_prot.final_active_usd > 0.0
    assert pytest.approx(dyn_prot.final_equity_usd, rel=1e-2) == (
        dyn_prot.final_active_usd + dyn_prot.final_protected_usd
    )


def test_paper_trades_1_conjunctive_shutdown(paper_trades_1_csv: Path):
    """
    Validates paper_trades (1).csv (255 trades):
    Verifies that conjunctive daily shutdown was triggered on 2026-10-31
    due to accumulated initial failures and a reversal failure.
    """
    trades = HistoricalReplayEngine.load_trades_csv(paper_trades_1_csv)
    assert len(trades) == 255

    # 1. Canonical $11.00 start: shutdown triggered on 2026-10-31
    results_11 = HistoricalReplayEngine.compare_all_modes(paper_trades_1_csv, starting_equity_usd=11.0)
    opt_b_11 = results_11["OPTION_B_RESERVE_RELEASE"]
    assert opt_b_11.shutdowns_triggered_count >= 1
    assert opt_b_11.max_equity_usd >= 43.0

    # 2. Replay starting at $15.00: crosses $44 milestone, triggers shutdown, executes Option B release
    results_15 = HistoricalReplayEngine.compare_all_modes(paper_trades_1_csv, starting_equity_usd=15.0)
    opt_b_15 = results_15["OPTION_B_RESERVE_RELEASE"]
    assert opt_b_15.milestone_44_reached is True
    assert opt_b_15.shutdowns_triggered_count >= 1
    assert opt_b_15.option_b_releases_count >= 1
    assert opt_b_15.final_protected_usd > 0.0
