#!/usr/bin/env python3
"""
CLI entry point for the backtester.
Replays recorded windows through production strategy and execution engines.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from polybot.backtest.engine import BacktestEngine
from polybot.backtest.loader import WindowDataLoader
from polybot.backtest.report import BacktestReporter
from polybot.backtest.sweep import ParameterSweeper
from polybot.config import PolybotConfig
from polybot.strategy import EdgeStrategy, LegacyStrategy


def main():
    parser = argparse.ArgumentParser(description="Polymarket Historical Backtester")
    parser.add_argument("--data", type=str, default="data/raw", help="Directory of raw window folders or .tar.gz archives")
    parser.add_argument("--strategy", choices=["legacy", "edge"], default="edge", help="Strategy to test")
    parser.add_argument("--sweep", action="store_true", help="Run full parameter grid sweep with hold-out validation")
    parser.add_argument("--stake", type=float, default=5.0, help="Stake size in USD")
    parser.add_argument("--min-edge", type=float, default=0.02, help="Minimum edge threshold")
    parser.add_argument("--early-exit", action="store_true", help="Enable early exit rule")
    args = parser.parse_args()

    cfg = PolybotConfig.load()
    cfg.strategy.strategy_name = args.strategy
    cfg.strategy.stake_usd = args.stake
    cfg.strategy.min_edge = args.min_edge
    cfg.strategy.early_exit = args.early_exit

    loader = WindowDataLoader(args.data)
    window_paths = loader.discover_windows()
    if not window_paths:
        print(f"No window data found in {args.data}. Run the recorder first: 'python main.py recorder'")
        sys.exit(0)

    print(f"Discovered {len(window_paths)} recorded windows in {args.data}. Loading...")
    windows = []
    for p in window_paths:
        win = loader.load_window(p)
        if win and win.winner:
            windows.append(win)

    print(f"Successfully parsed {len(windows)} settled windows with complete telemetry.")

    if args.sweep:
        print("\nStarting Parameter Grid Sweep with 70/30 Hold-Out Split...")
        sweeper = ParameterSweeper(cfg)
        results = sweeper.run_sweep(windows, train_ratio=0.70)
        print("\n" + ParameterSweeper.format_sweep_table(results))
        return

    # Single strategy backtest
    strat = LegacyStrategy(cfg.strategy) if args.strategy == "legacy" else EdgeStrategy(cfg.strategy)
    engine = BacktestEngine(cfg)
    trades = engine.run(windows, strategy=strat)

    report = BacktestReporter.generate_full_report(trades, strategy_name=args.strategy)
    print(report)


if __name__ == "__main__":
    main()
