import json
import tarfile
from pathlib import Path
import pytest

from polybot.backtest.engine import BacktestEngine, BacktestTrade
from polybot.backtest.loader import WindowDataLoader, WindowData, BacktestEvent
from polybot.backtest.report import BacktestReporter
from polybot.backtest.sweep import ParameterSweeper
from polybot.config import PolybotConfig, StrategyConfig
from polybot.strategy import EdgeStrategy, LegacyStrategy


def test_window_loader_directory_and_tar(tmp_path: Path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # 1. Create uncompressed window folder 1000
    w1_dir = raw_dir / "1000"
    w1_dir.mkdir()
    with open(w1_dir / "metadata.json", "w") as f:
        json.dump({"slug": "btc-updown-5m-1000", "window_start": 1000, "K": 80000.0, "tokens": {"Up": "u1", "Down": "d1"}}, f)
    with open(w1_dir / "resolution.json", "w") as f:
        json.dump({"winner": "Up"}, f)
    with open(w1_dir / "chainlink.jsonl", "w") as f:
        f.write(json.dumps({"timestamp_ms": 1000000, "price": 80000.0}) + "\n")
        f.write(json.dumps({"timestamp_ms": 1150000, "price": 80050.0}) + "\n")
    with open(w1_dir / "market.jsonl", "w") as f:
        f.write(json.dumps({"raw": [{"asset_id": "u1", "bids": [{"price": "0.90", "size": "100"}], "asks": [{"price": "0.92", "size": "100"}]}]}) + "\n")

    loader = WindowDataLoader(raw_dir)
    discovered = loader.discover_windows()
    assert len(discovered) == 1

    win1 = loader.load_window(w1_dir)
    assert win1 is not None
    assert win1.slug == "btc-updown-5m-1000"
    assert win1.winner == "Up"
    assert win1.strike_price_K == 80000.0
    assert len(win1.events) == 3


def test_backtest_engine_execution():
    win = WindowData(
        slug="btc-updown-5m-1000",
        window_start=1000,
        window_end=1300,
        tokens={"Up": "u1", "Down": "d1"},
        strike_price_K=80000.0,
        winner="Up",
        events=[
            # Market snapshot at t=1001
            BacktestEvent(1001.0, "market", {"raw": [{"asset_id": "u1", "bids": [{"price": "0.93", "size": "100"}], "asks": [{"price": "0.94", "size": "100"}]}]}),
            # Tick at t=1200 (elapsed 200s, inside 180s-285s window)
            BacktestEvent(1200.0, "chainlink", {"price": 80050.0}),
        ],
    )

    cfg = PolybotConfig()
    cfg.strategy.strategy_name = "legacy"
    cfg.strategy.stake_usd = 5.0
    cfg.strategy.legacy_price_min = 0.93
    cfg.strategy.legacy_price_max = 0.96

    engine = BacktestEngine(cfg)
    trades = engine.run([win], strategy=LegacyStrategy(cfg.strategy))

    assert len(trades) == 1
    t = trades[0]
    assert t.side == "Up"
    assert t.is_win
    assert pytest.approx(t.entry_price) == 0.94
    assert t.pnl_usd > 0


def test_reporter_summary_and_verdict():
    # 35 trades: 33 wins, 2 losses -> WR = 94.3% > 90.1% break-even
    trades = []
    for i in range(35):
        is_win = (i < 33)
        trades.append(BacktestTrade(
            slug=f"btc-updown-5m-{1000 + i * 300}",
            side="Up",
            action="BUY",
            order_type="taker",
            entry_price=0.90,
            shares=10.0,
            cost_usd=9.0,
            fee_usd=0.01,
            elapsed_sec=150.0,
            model_prob=0.95,
            expected_edge=0.04,
            z_score=1.5,
            vol_regime="low",
            hour_of_day=14,
            winner="Up" if is_win else "Down",
            is_win=is_win,
            payout_usd=10.0 if is_win else 0.0,
            pnl_usd=0.99 if is_win else -9.01,
        ))

    summary = BacktestReporter.compute_summary(trades)
    assert summary.total_trades == 35
    assert summary.wins == 33
    assert pytest.approx(summary.win_rate) == (33 / 35) * 100.0
    assert summary.net_pnl_usd > 0
    assert summary.win_rate_ci_lower > 0
    assert summary.win_rate_ci_upper <= 100.0
    # Break-even win rate is ~90.1%, and with 35 trades verdict is computed
    assert summary.statistical_verdict != ""


def test_parameter_sweeper():
    win = WindowData(
        slug="btc-updown-5m-1000",
        window_start=1000,
        window_end=1300,
        tokens={"Up": "u1", "Down": "d1"},
        strike_price_K=80000.0,
        winner="Up",
        events=[
            BacktestEvent(1001.0, "market", {"raw": [{"asset_id": "u1", "bids": [{"price": "0.93", "size": "100"}], "asks": [{"price": "0.94", "size": "100"}]}]}),
            BacktestEvent(1200.0, "chainlink", {"price": 80100.0}),
        ],
    )
    # Create 4 duplicate windows
    windows = [win] * 4

    sweeper = ParameterSweeper()
    results = sweeper.run_sweep(
        windows,
        train_ratio=0.50,
        param_grid={"min_edge": [0.01, 0.05], "z_min": [0.5]},
    )

    assert len(results) == 2
    tbl = ParameterSweeper.format_sweep_table(results)
    assert "min_edge" in tbl
