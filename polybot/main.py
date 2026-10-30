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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    from polybot.dashboard.server import DashboardServer
    from polybot.dashboard.state import DashboardState
    from polybot.execution.base import Order
    from polybot.execution.ledger import TradeLedger
    from polybot.execution.paper import PaperExecutionEngine
    from polybot.feeds.binance import BinanceFeed
    from polybot.feeds.clob_ws import CLOBOrderBookFeed
    from polybot.feeds.health import FeedHealthTracker
    from polybot.feeds.rtds import RTDSFeed
    from polybot.gamma import PolymarketClient
    from polybot.risk import RiskManager
    from polybot.session import MarketSessionManager
    from polybot.sizing import PositionSizer
    from polybot.strategy import EdgeStrategy, LegacyStrategy, ReversalStrategy

    log.info(
        "PAPER MODE ACTIVE | strategy=%s initial_stake=$%.2f reversal_stake=$%.2f latency=%dms",
        config.strategy.strategy_name,
        config.strategy.initial_stake_usd,
        config.strategy.reversal_stake_usd,
        config.strategy.latency_ms,
    )

    ledger = TradeLedger(config.recorder.data_dir / "paper_ledger.db")
    engine = PaperExecutionEngine(config.strategy, ledger=ledger)
    client = PolymarketClient(config.feeds.gamma_rest_url, config.feeds.clob_rest_url)
    health = FeedHealthTracker(config.feeds.max_feed_staleness_sec, max_divergence_pct=config.feeds.max_divergence_pct)
    risk = RiskManager(config.risk)
    sizer = PositionSizer(config.risk, default_stake_usd=config.strategy.stake_usd)
    session_mgr = MarketSessionManager(
        window_duration_sec=300.0,
        waiting_period_sec=config.strategy.reversal_window_start_sec
    )

    dash_state = DashboardState()
    dash_state.set_bot_status("RUNNING")
    dash_state.strategy_name = config.strategy.strategy_name

    dash_server = None
    if config.dashboard.enabled:
        dash_server = DashboardServer(
            dash_state,
            ledger,
            host=config.dashboard.host,
            port=config.dashboard.port,
        )
        await dash_server.start()

    if config.strategy.strategy_name == "legacy":
        strat = LegacyStrategy(config.strategy)
    elif config.strategy.strategy_name == "reversal":
        strat = ReversalStrategy(config.strategy)
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
    is_paused = False

    dash_state.add_log("INFO", f"Bot initialized with {config.strategy.strategy_name} strategy")

    try:
        while True:
            # Handle UI controls
            action = dash_state.pop_requested_action()
            if action == "PAUSE":
                is_paused = True
                dash_state.set_bot_status("PAUSED")
                log.info("Paper trading PAUSED via dashboard control")
            elif action in ("RESUME", "START"):
                is_paused = False
                dash_state.set_bot_status("RUNNING")
                log.info("Paper trading RESUMED via dashboard control")
            elif action == "STOP":
                dash_state.set_bot_status("STOPPED")
                log.info("Paper trading STOPPED via dashboard control")
                break

            if risk.is_kill_switch_active():
                dash_state.set_bot_status("STOPPED")
                log.warning("Kill switch file '%s' detected! Halting paper trading.", config.risk.kill_switch_file)
                break

            now = time.time()
            time_state = session_mgr.sync(now)
            window_start = int(time_state.window_start_ts)
            current_window_idx = window_start // 300
            current_slug = f"btc-updown-5m-{window_start}"

            # Window rollover
            if current_window_start != window_start:
                current_window_start = window_start
                m_info = client.get_market(window_start)
                if m_info and m_info.get("tokens"):
                    clob.track_tokens(m_info["tokens"])
                    K_val = health.chainlink.last_price
                    strat.reset_window(window_start, K_val)
                    dash_state.add_log("INFO", f"Rollover into market {current_slug}")
                    log.info("New window %d: strike K=%s, tokens=%s", window_start, f"${K_val:.2f}" if K_val else "pending", m_info["tokens"])

            # Process pending latency fills
            new_fills = engine.process_pending_orders(now, clob.books)
            for f in new_fills:
                dash_state.add_log(
                    "BUY",
                    f"Paper fill: {f.signal_type} {f.side} {f.shares:.2f}sh @ ${f.price:.3f} (${f.cost_usd:.2f})"
                )

            # Update dashboard market clock & state
            dash_state.update_clock(
                slug=current_slug,
                window_start=window_start,
                window_end=int(time_state.window_end_ts),
                elapsed_sec=time_state.elapsed_sec,
                remaining_sec=time_state.remaining_sec,
                phase=time_state.phase.value,
                is_trading_allowed=time_state.is_trading_allowed,
            )

            # Extract book state
            book_up = clob.get_book("Up")
            book_down = clob.get_book("Down")
            up_ask = book_up.best_ask() if book_up else None
            down_ask = book_down.best_ask() if book_down else None
            up_bid = book_up.best_bid() if book_up else None
            down_bid = book_down.best_bid() if book_down else None

            # Check if CLOB WS has books; if not, poll rest fallback
            if (up_ask is None or down_ask is None) and clob._subscribed_tokens:
                clob.poll_rest_fallback()
                book_up = clob.get_book("Up")
                book_down = clob.get_book("Down")
                up_ask = book_up.best_ask() if book_up else None
                down_ask = book_down.best_ask() if book_down else None

            dash_state.update_probabilities(
                up_prob=up_ask,
                down_prob=down_ask,
                up_bid=up_bid,
                down_bid=down_bid,
                latency_ms=config.strategy.latency_ms,
            )

            # Check trigger range status for UI display
            if isinstance(strat, ReversalStrategy):
                initial_met = (
                    (up_ask is not None and config.strategy.initial_prob_min <= up_ask <= config.strategy.initial_prob_max) or
                    (down_ask is not None and config.strategy.initial_prob_min <= down_ask <= config.strategy.initial_prob_max)
                )
                reversal_met = False
                if strat.initial_order_placed and strat.initial_side:
                    opp_prob = down_ask if strat.initial_side == "Up" else up_ask
                    if opp_prob is not None:
                        reversal_met = config.strategy.reversal_prob_min <= opp_prob <= config.strategy.reversal_prob_max

                dash_state.update_strategy_state(
                    initial_met=initial_met,
                    reversal_met=reversal_met,
                    initial_triggered=strat.initial_order_placed,
                    reversal_triggered=strat.reversal_order_placed,
                    initial_side=strat.initial_side,
                )

            # Check for trade signal if not paused
            if not is_paused:
                cur_price = health.chainlink.last_price or 0.0
                sig = strat.on_tick(cur_price, now, clob.books, health)
                if sig:
                    stake_usd = sig.cost_usd
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
                            signal_type=sig.signal_type,
                            linked_order_id=sig.linked_order_id,
                            trigger_prob=sig.trigger_prob,
                            counterpart_prob=sig.counterpart_prob,
                            data_receipt_ts=sig.data_receipt_ts,
                        )
                        engine.submit_order(order)
                        risk.record_trade_placed(stake_usd)
                        open_trades_to_settle.append({
                            "slug": sig.slug,
                            "cost": stake_usd,
                            "window_idx": current_window_idx,
                        })
                        dash_state.add_log(
                            "SIGNAL",
                            f"Signal triggered: {sig.signal_type} {sig.side} (${stake_usd:.2f}) prob={sig.trigger_prob:.3f}"
                        )
                    else:
                        log.warning("Trade rejected by Risk Manager: %s", reason)
                        dash_state.add_log("WARNING", f"Risk rejected trade: {reason}")

            # Check settlements
            for item in list(open_trades_to_settle):
                slug = item["slug"]
                winner = client.get_winner(slug)
                if winner:
                    settled_pos = engine.settle_window(slug, winner, now)
                    if settled_pos:
                        risk.record_trade_settled(settled_pos.realized_pnl_usd, item["cost"], item["window_idx"])
                        dash_state.add_log(
                            "SETTLED",
                            f"Settled {slug}: winner={winner} | PnL=${settled_pos.realized_pnl_usd:+.2f}"
                        )
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
        dash_state.set_bot_status("STOPPED")
        if dash_server:
            await dash_server.stop()
        await rtds.stop()
        await binance.stop()
        await clob.stop()
        log.info("Paper engine stopped. Total realized PnL: $%+.2f", engine.get_total_realized_pnl())


async def run_dashboard(config: PolybotConfig) -> None:
    from polybot.dashboard.server import DashboardServer
    from polybot.dashboard.state import DashboardState
    from polybot.execution.ledger import TradeLedger

    ledger = TradeLedger(config.recorder.data_dir / "paper_ledger.db")
    state = DashboardState()
    state.set_bot_status("STANDALONE_VIEW")
    server = DashboardServer(state, ledger, host=config.dashboard.host, port=config.dashboard.port)
    await server.start()
    log.info("Dashboard active at http://%s:%d. Press Ctrl+C to exit.", config.dashboard.host, config.dashboard.port)
    try:
        while True:
            await asyncio.sleep(1.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m Trading & Recording System")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command mode to run")

    # Recorder command
    rec_parser = subparsers.add_parser("recorder", help="Run 24/7 market data recorder")
    rec_parser.add_argument("--data-dir", type=str, default=None, help="Directory to store raw tick data")
    rec_parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")

    # Paper trading command
    paper_parser = subparsers.add_parser("paper", help="Run paper trading bot")
    paper_parser.add_argument("--strategy", choices=["reversal", "legacy", "edge"], default="reversal", help="Strategy to run")
    paper_parser.add_argument("--stake", type=float, default=None, help="Stake in USD (for edge/legacy)")
    paper_parser.add_argument("--port", type=int, default=8080, help="Local dashboard port")
    paper_parser.add_argument("--no-dashboard", action="store_true", help="Run headless without dashboard server")

    # Dashboard standalone command
    dash_parser = subparsers.add_parser("dashboard", help="Run local web dashboard standalone")
    dash_parser.add_argument("--port", type=int, default=8080, help="Local dashboard port")
    dash_parser.add_argument("--host", type=str, default="127.0.0.1", help="Local dashboard host")

    # Backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run historical backtester")
    bt_parser.add_argument("--data", type=str, default="data/raw", help="Path to raw window data or polyticker data")
    bt_parser.add_argument("--strategy", choices=["reversal", "legacy", "edge"], default="reversal", help="Strategy to evaluate")

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
        if args.stake is not None:
            config.strategy.stake_usd = args.stake
        if args.port:
            config.dashboard.port = args.port
        if args.no_dashboard:
            config.dashboard.enabled = False
        try:
            asyncio.run(run_paper(config))
        except KeyboardInterrupt:
            log.info("Stopped by user.")
    elif args.command == "dashboard":
        if args.port:
            config.dashboard.port = args.port
        if args.host:
            config.dashboard.host = args.host
        try:
            asyncio.run(run_dashboard(config))
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
        from polybot.strategy import EdgeStrategy, LegacyStrategy, ReversalStrategy

        loader = WindowDataLoader(args.data)
        window_paths = loader.discover_windows()
        if not window_paths:
            log.warning("No window data found in %s. Run 'python main.py recorder' first.", args.data)
            return

        log.info("Loading %d window archives from %s...", len(window_paths), args.data)
        windows = [loader.load_window(p) for p in window_paths]
        valid_windows = [w for w in windows if w and w.winner]

        config.strategy.strategy_name = args.strategy
        if args.strategy == "legacy":
            strat = LegacyStrategy(config.strategy)
        elif args.strategy == "reversal":
            strat = ReversalStrategy(config.strategy)
        else:
            strat = EdgeStrategy(config.strategy)

        engine = BacktestEngine(config)
        trades = engine.run(valid_windows, strategy=strat)
        print(BacktestReporter.generate_full_report(trades, strategy_name=args.strategy))


if __name__ == "__main__":
    main()
