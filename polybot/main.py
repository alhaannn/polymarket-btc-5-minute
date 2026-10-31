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

async def run_paper(config: PolybotConfig, demo: bool = False) -> None:
    import math
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
    from polybot.capital import CapitalManager, DynamicTradeBudgetCalculator
    from polybot.risk import RiskManager
    from polybot.session import MarketSessionManager
    from polybot.sizing import PositionSizer
    from polybot.strategy import EdgeStrategy, LegacyStrategy, ReversalStrategy

    capital_mgr = CapitalManager(db_path=config.recorder.data_dir / "paper_ledger.db", starting_equity_usd=11.0)
    budget_calc = DynamicTradeBudgetCalculator(
        min_order_usd=1.0,
        taker_fee_rate=config.strategy.taker_fee_rate
    )

    log.info(
        "PAPER MODE ACTIVE | strategy=%s initial_stake=$%.2f reversal_stake=$%.2f latency=%dms demo=%s",
        config.strategy.strategy_name,
        config.strategy.initial_stake_usd,
        config.strategy.reversal_stake_usd,
        config.strategy.latency_ms,
        demo,
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

    rtds = None
    binance = None
    clob = CLOBOrderBookFeed(ws_url=config.feeds.clob_ws_url, rest_client=client)

    if demo:
        log.info("DEMO MODE ENABLED: Simulating 5-minute market cycles and probability swings for live UI workstation testing.")
        dash_state.add_log("INFO", "Demo simulation active: realistic 5m cycle, initial + reversal triggers")
        clob.track_tokens({"Up": "demo-up", "Down": "demo-down"})
    else:
        rtds = RTDSFeed(
            ws_url=config.feeds.rtds_ws_url,
            ping_interval_sec=config.feeds.rtds_ping_interval_sec,
            on_tick=lambda t: health.record_tick("chainlink", t["price"], t["timestamp_ms"]),
        )
        binance = BinanceFeed(
            ws_url=config.feeds.binance_ws_url,
            on_tick=lambda t: health.record_tick("binance", t["price"], t["timestamp_ms"]),
        )
        await rtds.start()
        await binance.start()
        await clob.start()

    current_window_start: Optional[int] = None
    current_tokens: Optional[Dict[str, str]] = None
    last_discovery_attempt_ts = 0.0
    open_trades_to_settle: List[Dict[str, Any]] = []
    is_paused = False
    last_rest_poll_ts = 0.0

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
                current_tokens = None
                K_val = health.chainlink.last_price
                strat.reset_window(window_start, K_val)

            # Market discovery (demo or live)
            if demo:
                if not current_tokens:
                    current_tokens = {"Up": "demo-up", "Down": "demo-down"}
                    clob.track_tokens(current_tokens)
                    K_val = 68500.0
                    strat.reset_window(window_start, K_val)
                    health.record_tick("chainlink", K_val, int(now * 1000))
                    health.record_tick("binance", K_val, int(now * 1000))
                    dash_state.add_log("INFO", f"Demo rollover into window {window_start} (strike K=${K_val:,.2f})")
                    log.info("Demo rollover window %d: strike K=$%.2f", window_start, K_val)
            else:
                # Live network feeds: If tokens not yet discovered, attempt lookup (throttled every 2s)
                if not current_tokens and (now - last_discovery_attempt_ts > 2.0):
                    last_discovery_attempt_ts = now
                    try:
                        m_info = await asyncio.wait_for(
                            asyncio.to_thread(client.get_market, window_start),
                            timeout=2.0
                        )
                    except Exception as exc:
                        m_info = None
                        log.debug("Market discovery timed out for %s: %s", current_slug, exc)

                    if m_info and m_info.get("tokens"):
                        current_tokens = m_info["tokens"]
                        clob.track_tokens(current_tokens)
                        K_val = health.chainlink.last_price
                        strat.reset_window(window_start, K_val)
                        dash_state.add_log("INFO", f"Discovered market {current_slug} tokens")
                        log.info("Market %d discovered: strike K=%s, tokens=%s", window_start, f"${K_val:.2f}" if K_val else "pending", current_tokens)
                        # Immediately take initial REST snapshot so books are primed instantly!
                        try:
                            await asyncio.wait_for(asyncio.to_thread(clob.poll_rest_fallback), timeout=2.0)
                        except Exception:
                            pass
                    else:
                        dash_state.add_log("WARN", f"Market lookup pending for {current_slug} (retrying in 2s)")

            # In Demo mode, simulate price & probability curves
            if demo:
                elapsed = time_state.elapsed_sec
                base_k = 68500.0
                if elapsed < 180:
                    # 0-179s: WAITING phase, balanced market ~0.50
                    up_p = 0.51 + 0.03 * math.sin(elapsed / 15.0)
                    down_p = 1.0 - up_p
                    cur_btc = base_k + 12.0 * math.sin(elapsed / 10.0)
                elif 180 <= elapsed < 215:
                    # 180-214s: TRADING phase - Up surges into 89-92% trigger zone
                    up_p = 0.905
                    down_p = 0.095
                    cur_btc = base_k + 75.0
                elif 215 <= elapsed < 275:
                    # 215-274s: Sharp reversal! Down surges into 85-90% trigger zone
                    up_p = 0.125
                    down_p = 0.875
                    cur_btc = base_k - 55.0
                else:
                    # 275-299s: Down strengthens to 95%
                    up_p = 0.050
                    down_p = 0.950
                    cur_btc = base_k - 85.0

                health.record_tick("chainlink", cur_btc, int(now * 1000))
                health.record_tick("binance", cur_btc, int(now * 1000))

                clob.books["demo-up"].apply_snapshot(
                    [{"price": round(up_p - 0.01, 3), "size": 500}],
                    [{"price": round(up_p, 3), "size": 500}]
                )
                clob.books["demo-down"].apply_snapshot(
                    [{"price": round(down_p - 0.01, 3), "size": 500}],
                    [{"price": round(down_p, 3), "size": 500}]
                )

            # Process pending latency fills (passing side_books mapping)
            new_fills = engine.process_pending_orders(now, clob.side_books)
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

            # Extract book state using effective best ask/bid (including prediction market complement)
            up_ask = clob.get_effective_ask("Up")
            down_ask = clob.get_effective_ask("Down")
            up_bid = clob.get_effective_bid("Up")
            down_bid = clob.get_effective_bid("Down")

            # Check if order books are empty or stale (>1.5s since last tick); if so, poll REST fallback
            if not demo and clob._subscribed_tokens:
                newest_update_ts = max(
                    (b.last_update_ts for b in clob.books.values() if b.last_update_ts > 0),
                    default=0.0
                )
                book_is_stale = (now - newest_update_ts > 1.5)
                books_missing = (up_ask is None or down_ask is None)

                if (books_missing or book_is_stale) and (now - last_rest_poll_ts > 1.5):
                    last_rest_poll_ts = now
                    try:
                        await asyncio.wait_for(asyncio.to_thread(clob.poll_rest_fallback), timeout=1.5)
                        up_ask = clob.get_effective_ask("Up")
                        down_ask = clob.get_effective_ask("Down")
                        up_bid = clob.get_effective_bid("Up")
                        down_bid = clob.get_effective_bid("Down")
                    except Exception:
                        pass

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

            # Check for trade signal if not paused (passing side_books)
            if not is_paused:
                cur_price = health.chainlink.last_price or 0.0

                if isinstance(strat, ReversalStrategy):
                    init_b, rev_b = budget_calc.get_budgets(capital_mgr.state.active_capital)
                    strat.set_dynamic_budgets(init_b, rev_b)

                sig = strat.on_tick(cur_price, now, clob.side_books, health)
                if sig:
                    stake_usd = sig.cost_usd
                    risk.sync_open_exposure(engine.get_open_exposure())

                    # Pre-trade dynamic budget & free active funds validation
                    b_val = budget_calc.validate_order_budget(
                        signal_type=sig.signal_type,
                        active_capital_usd=capital_mgr.state.active_capital,
                        open_exposure_usd=engine.get_open_exposure(),
                    )
                    if not b_val.allowed:
                        if hasattr(strat, "on_order_rejected"):
                            strat.on_order_rejected(sig.signal_type)
                        log.warning("Order budget rejected: %s", b_val.reason)
                        dash_state.add_log("WARNING", f"Budget rejected: {b_val.reason}")
                        continue

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
                            "window_end": (current_window_idx + 1) * 300,
                            "last_checked_ts": 0.0,
                        })
                        dash_state.add_log(
                            "SIGNAL",
                            f"Signal triggered: {sig.signal_type} {sig.side} (${stake_usd:.2f}) prob={sig.trigger_prob:.3f}"
                        )
                    else:
                        if hasattr(strat, "on_order_rejected"):
                            strat.on_order_rejected(sig.signal_type)
                        log.warning("Trade rejected by Risk Manager: %s", reason)
                        dash_state.add_log("WARNING", f"Risk rejected trade: {reason}")

            # Check settlements for concluded windows
            pending_slugs = {item["slug"] for item in open_trades_to_settle if now >= item.get("window_end", 0)}
            for slug in pending_slugs:
                if demo:
                    winner = "Down"
                else:
                    last_check = next((it.get("last_checked_ts", 0.0) for it in open_trades_to_settle if it["slug"] == slug), 0.0)
                    if now - last_check < 10.0:
                        continue
                    for it in open_trades_to_settle:
                        if it["slug"] == slug:
                            it["last_checked_ts"] = now
                    try:
                        winner = await asyncio.wait_for(
                            asyncio.to_thread(client.get_winner, slug),
                            timeout=2.0
                        )
                    except Exception:
                        winner = None

                if winner:
                    settled_positions = engine.settle_window_all(slug, winner, now)
                    for settled_pos in settled_positions:
                        risk.record_trade_settled(settled_pos.realized_pnl_usd, settled_pos.total_cost_usd, current_window_idx)
                        dash_state.add_log(
                            "SETTLED",
                            f"Settled {slug}: position={settled_pos.side} ({settled_pos.signal_type}) winner={winner} | PnL=${settled_pos.realized_pnl_usd:+.2f}"
                        )
                        log.info(
                            "PAPER POSITION SETTLED %s %s (%s) | PnL=$%+.2f | Total PnL=$%+.2f",
                            slug,
                            settled_pos.side,
                            settled_pos.signal_type,
                            settled_pos.realized_pnl_usd,
                            engine.get_total_realized_pnl(),
                        )
                    open_trades_to_settle = [it for it in open_trades_to_settle if it["slug"] != slug]
                    risk.sync_open_exposure(engine.get_open_exposure())

            await asyncio.sleep(0.5)

    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Stopping paper trading...")
    finally:
        dash_state.set_bot_status("STOPPED")
        if dash_server:
            await dash_server.stop()
        if not demo:
            if rtds:
                await rtds.stop()
            if binance:
                await binance.stop()
            if clob:
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
    paper_parser.add_argument("--host", type=str, default=None, help="Dashboard host (e.g. 0.0.0.0 for VPS access)")
    paper_parser.add_argument("--no-dashboard", action="store_true", help="Run headless without dashboard server")
    paper_parser.add_argument("--demo", action="store_true", help="Simulate 5m market price & probability swings to test UI and strategy")

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
        if getattr(args, "host", None):
            config.dashboard.host = args.host
        if args.no_dashboard:
            config.dashboard.enabled = False
        try:
            asyncio.run(run_paper(config, demo=getattr(args, "demo", False)))
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
