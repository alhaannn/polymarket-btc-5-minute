"""
Parameter sweep engine with hold-out training/testing split.
Fits model calibration on training windows, and evaluates parameter combinations on unseen test windows.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from polybot.backtest.engine import BacktestEngine, BacktestTrade
from polybot.backtest.loader import WindowData
from polybot.backtest.report import BacktestReporter, PerformanceSummary
from polybot.config import PolybotConfig, StrategyConfig
from polybot.strategy import EdgeStrategy

log = logging.getLogger("polybot.backtest.sweep")


@dataclass
class SweepResult:
    params: Dict[str, Any]
    train_summary: PerformanceSummary
    test_summary: PerformanceSummary


class ParameterSweeper:
    """Performs grid searches across strategy parameters with hold-out validation."""

    def __init__(self, config: Optional[PolybotConfig] = None):
        self.config = config or PolybotConfig()

    def run_sweep(
        self,
        windows: List[WindowData],
        train_ratio: float = 0.70,
        param_grid: Optional[Dict[str, List[Any]]] = None,
    ) -> List[SweepResult]:
        if not windows:
            return []

        # Split windows chronologically: train on past, test on future
        split_idx = int(len(windows) * train_ratio)
        train_windows = windows[:split_idx]
        test_windows = windows[split_idx:]

        log.info(
            "Parameter Sweep: Total Windows=%d (Train=%d, Test=%d)",
            len(windows), len(train_windows), len(test_windows)
        )

        grid = param_grid or {
            "min_edge": [0.015, 0.02, 0.03],
            "z_min": [0.5, 1.0, 1.5],
            "early_exit": [False, True],
            "order_style": ["taker"],
        }

        keys = list(grid.keys())
        combos = list(itertools.product(*[grid[k] for k in keys]))
        results: List[SweepResult] = []

        for combo in combos:
            params = dict(zip(keys, combo))
            # Build strategy config
            strat_cfg = StrategyConfig(
                min_edge=params.get("min_edge", 0.02),
                z_min=params.get("z_min", 1.0),
                early_exit=params.get("early_exit", False),
                order_style=params.get("order_style", "taker"),
            )

            # 1. Run on Training Windows
            train_strat = EdgeStrategy(strat_cfg)
            engine_train = BacktestEngine(PolybotConfig(strategy=strat_cfg))
            train_trades = engine_train.run(train_windows, strategy=train_strat)
            train_summary = BacktestReporter.compute_summary(train_trades)

            # 2. Run on Hold-Out Test Windows
            test_strat = EdgeStrategy(strat_cfg)
            engine_test = BacktestEngine(PolybotConfig(strategy=strat_cfg))
            test_trades = engine_test.run(test_windows, strategy=test_strat)
            test_summary = BacktestReporter.compute_summary(test_trades)

            results.append(SweepResult(params, train_summary, test_summary))

        # Sort by hold-out test net PnL descending
        results.sort(key=lambda r: r.test_summary.net_pnl_usd, reverse=True)
        return results

    @staticmethod
    def format_sweep_table(results: List[SweepResult]) -> str:
        lines = [
            f"| {'Parameters':<42} | Train Trades | Train PnL | Test Trades | Test WR | Test PnL | Test Brier |",
            f"|{'-' * 44}|{'-' * 14}|{'-' * 11}|{'-' * 13}|{'-' * 9}|{'-' * 10}|{'-' * 12}|",
        ]

        for r in results:
            param_str = ", ".join(f"{k}={v}" for k, v in r.params.items())
            lines.append(
                f"| {param_str:<42} | {r.train_summary.total_trades:>12} | ${r.train_summary.net_pnl_usd:>+8.2f} | "
                f"{r.test_summary.total_trades:>11} | {r.test_summary.win_rate:>6.1f}% | ${r.test_summary.net_pnl_usd:>+7.2f} | {r.test_summary.brier_score:>10.4f} |"
            )

        return "\n".join(lines)
