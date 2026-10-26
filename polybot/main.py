"""
Main entry point and CLI runner for polybot.
Supports modes: recorder, paper, backtest, live.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from polybot.config import PolybotConfig
from polybot.recorder import MarketRecorder

log = logging.getLogger("polybot")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def run_recorder(config: PolybotConfig) -> None:
    recorder = MarketRecorder(config)
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _sig_handler() -> None:
        log.info("Termination signal received. Shutting down gracefully...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except (NotImplementedError, RuntimeError):
            # Windows signal handler fallback
            pass

    await recorder.start()
    log.info("24/7 Market Recorder active. Saving to %s", config.recorder.raw_dir)

    try:
        while not stop_event.is_set():
            if Path(config.risk.kill_switch_file).exists():
                log.warning("Kill switch file '%s' detected! Stopping recorder.", config.risk.kill_switch_file)
                break
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await recorder.stop()
        log.info("Recorder shut down complete.")

async def run_paper(config: PolybotConfig) -> None:
    from polybot.execution.base import Order
    from polybot.execution.paper import PaperExecutionEngine
    from polybot.feeds.binance import BinanceFeed
    from polybot.feeds.clob_ws import CLOBOrderBookFeed
    from polybot.feeds.health import FeedHealthTracker
    from polybot.feeds.rtds import RTDSFeed
    from polybot.gamma import PolymarketClient
    from polybot.risk import RiskManager
    from polybot.sizing import PositionSizer
    from polybot.strategy import EdgeStrategy, LegacyStrategy

    log.info(
        "PAPER MODE ACTIVE | strategy=%s stake=$%.2f range=[%.2f, %.2f] latency=%dms",
        config.strategy.strategy_name,
        config.strategy.stake_usd,
        config.strategy.price_min,
        config.strategy.price_max,
        config.strategy.latency_ms,
    )

    client = PolymarketClient(config.feeds.gamma_rest_url, config.feeds.clob_rest_url)
    health = FeedHealthTracker(config.feeds.max_feed_staleness_sec, max_divergence_pct=config.feeds.max_divergence_pct)
    risk = RiskManager(config.risk)
    sizer = PositionSizer(config.risk, default_stake_usd=config.strategy.stake_usd)
    engine = PaperExecutionEngine(config.strategy)

    if config.strategy.strategy_name == "legacy":
        strat = LegacyStrategy(config.strategy)
    else:
        strat = EdgeStrategy(config.strategy)

    rtds = RTDSFeed(
        ws_url=config.feeds.rtds_ws_url,
        ping_interval_sec=config.feeds.rtds_ping_interval_sec,
        on_tick=lambda t: health.record_tick("chainlink", t["price"], t["timestamp_ms"]),
    )
    binance = BinanceFeed(
        ws_url=config.feeds.binance_ws_url,
        on_tick=lambda t: health.record_tick("binance", t["price"], t["timestamp_ms"]),
    )
    clob = CLOBOrderBookFeed(ws_url=config.feeds.clob_ws_url, rest_client=client)

    await rtds.start()
    await binance.start()
    await clob.start()

    current_window_start: Optional[int] = None
    open_trades_to_settle: List[Dict[str, Any]] = []

    try:
        while True:
            if risk.is_kill_switch_active():
                log.warning("Kill switch file '%s' detected! Halting paper trading.", config.risk.kill_switch_file)
                break

            now = time.time()
            now_sec = int(now)
            window_start = (now_sec // 300) * 300
            current_window_idx = window_start // 300

            # Window rollover
            if current_window_start != window_start:
                current_window_start = window_start
                m_info = client.get_market(window_start)
                if m_info and m_info.get("tokens"):
                    clob.track_tokens(m_info["tokens"])
                    K_val = health.chainlink.last_price
                    strat.reset_window(window_start, K_val)
                    log.info("New window %d: strike K=%s, tokens=%s", window_start, f"${K_val:.2f}" if K_val else "pending", m_info["tokens"])

            # Process pending latency fills
            engine.process_pending_orders(now, clob.books)

            # Check for trade signal
            cur_price = health.chainlink.last_price or 0.0
            if cur_price > 0:
                sig = strat.on_tick(cur_price, now, clob.books, health)
                if sig:
                    stake_usd = sizer.calculate_stake(sig.expected_edge, sig.model_prob, sig.price)
                    allowed, reason = risk.check_trade_allowed(stake_usd, current_window_idx)
                    if allowed:
                        order = Order(
                            order_id=f"paper-{int(now * 1000)}",
                            slug=sig.slug,
                            side=sig.side,
                            action=sig.action,
                            order_type=sig.order_type,
                            price=sig.price,
                            usd_amount=stake_usd,
                            shares=sig.shares,
                            created_at_ts=now,
                            latency_ms=config.strategy.latency_ms,
                        )
                        engine.submit_order(order)
                        risk.record_trade_placed(stake_usd)
                        open_trades_to_settle.append({"slug": sig.slug, "cost": stake_usd, "window_idx": current_window_idx})
                    else:
                        log.warning("Trade rejected by Risk Manager: %s", reason)

            # Check settlements
            for item in list(open_trades_to_settle):
                slug = item["slug"]
                winner = client.get_winner(slug)
                if winner:
                    settled_pos = engine.settle_window(slug, winner, now)
                    if settled_pos:
                        risk.record_trade_settled(settled_pos.realized_pnl_usd, item["cost"], item["window_idx"])
                        log.info(
                            "PAPER POSITION SETTLED | PnL=$%+.2f | Total PnL=$%+.2f",
                            settled_pos.realized_pnl_usd,
                            engine.get_total_realized_pnl(),
                        )
                    open_trades_to_settle.remove(item)

            await asyncio.sleep(0.5)

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Stopping paper trading...")
    finally:
        await rtds.stop()
        await binance.stop()
        await clob.stop()
        log.info("Paper engine stopped. Total realized PnL: $%+.2f", engine.get_total_realized_pnl())


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m Trading & Recording System")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command mode to run")

    # Recorder command
    rec_parser = subparsers.add_parser("recorder", help="Run 24/7 market data recorder")
    rec_parser.add_argument("--data-dir", type=str, default=None, help="Directory to store raw tick data")
    rec_parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")

    # Paper trading command
    paper_parser = subparsers.add_parser("paper", help="Run paper trading bot")
    paper_parser.add_argument("--strategy", choices=["legacy", "edge"], default="edge", help="Strategy to run")
    paper_parser.add_argument("--stake", type=float, default=5.0, help="Stake in USD")

    # Backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run historical backtester")
    bt_parser.add_argument("--data", type=str, default="data/raw", help="Path to raw window data or polyticker data")
    bt_parser.add_argument("--strategy", choices=["legacy", "edge"], default="edge", help="Strategy to evaluate")

    # Live trading command (strictly gated)
    live_parser = subparsers.add_parser("live", help="Run live trading (gated)")
    live_parser.add_argument("--live", action="store_true", help="Explicit confirmation flag for live trading")

    args = parser.parse_args()

    setup_logging(verbose=getattr(args, "verbose", False))
    config = PolybotConfig.load()

    if args.command == "recorder":
        if args.data_dir:
            config.recorder.raw_dir = Path(args.data_dir)
        try:
            asyncio.run(run_recorder(config))
        except KeyboardInterrupt:
            log.info("Stopped by user.")
    elif args.command == "paper":
        config.strategy.strategy_name = args.strategy
        if args.stake:
            config.strategy.stake_usd = args.stake
        try:
            asyncio.run(run_paper(config))
        except KeyboardInterrupt:
            log.info("Stopped by user.")
    elif args.command == "live":
        # Rule 1 verification
        config.live.is_live_requested = args.live
        if not config.live.is_live_authorized():
            log.error(
                "CRITICAL: Live mode denied! Both --live CLI flag AND env var "
                "LIVE_CONFIRM=YES_I_UNDERSTAND are required. Exiting."
            )
            sys.exit(1)
        log.info("Live mode authorized. (Live engine module will execute in Phase 5)")
    elif args.command == "backtest":
        from polybot.backtest.engine import BacktestEngine
        from polybot.backtest.loader import WindowDataLoader
        from polybot.backtest.report import BacktestReporter
        from polybot.strategy import EdgeStrategy, LegacyStrategy

        loader = WindowDataLoader(args.data)
        window_paths = loader.discover_windows()
        if not window_paths:
            log.warning("No window data found in %s. Run 'python main.py recorder' first.", args.data)
            return

        log.info("Loading %d window archives from %s...", len(window_paths), args.data)
        windows = [loader.load_window(p) for p in window_paths]
        valid_windows = [w for w in windows if w and w.winner]

        config.strategy.strategy_name = args.strategy
        strat = LegacyStrategy(config.strategy) if args.strategy == "legacy" else EdgeStrategy(config.strategy)
        engine = BacktestEngine(config)
        trades = engine.run(valid_windows, strategy=strat)
        print(BacktestReporter.generate_full_report(trades, strategy_name=args.strategy))


if __name__ == "__main__":
    main()
