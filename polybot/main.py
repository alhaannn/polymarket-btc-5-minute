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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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

async def run_paper(
    config: PolybotConfig,
    demo: bool = False,
    reset: bool = False,
    paused: bool = False,
    mode: Optional[str] = None,
) -> None:
    import math
    from polybot.tz import ist_today_str
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
    from polybot.capital import CapitalManager, DynamicTradeBudgetCalculator, CapitalReconciler
    from polybot.risk import RiskManager
    from polybot.session import MarketSessionManager
    from polybot.sizing import PositionSizer
    from polybot.strategy import EdgeStrategy, LegacyStrategy, ReversalStrategy

    sizing_mode = os.getenv("SIZING_MODE", "canonical")
    exec_mode = (mode or os.getenv("EXECUTION_MODE", "paper")).lower()
    is_live = exec_mode == "live"
    if is_live:
        config.live.is_live_requested = True
    db_name = "live_ledger.db" if is_live else "paper_ledger.db"
    mode_label = "LIVE TRADING" if is_live else "PAPER MODE"

    starting_equity_usd = float(os.getenv("STARTING_EQUITY_USD", "11.0"))
    capital_mgr = CapitalManager(
        db_path=config.recorder.data_dir / db_name,
        starting_equity_usd=starting_equity_usd,
        sizing_mode=sizing_mode,
    )
    budget_calc = DynamicTradeBudgetCalculator(
        min_order_usd=1.0,
        taker_fee_rate=config.strategy.taker_fee_rate,
        sizing_mode=sizing_mode,
    )

    ledger = TradeLedger(config.recorder.data_dir / db_name)

    dash_state = DashboardState()
    dash_state.execution_mode = exec_mode
    is_paused = paused or (os.getenv("PAUSED", "false").lower() in ("true", "1", "yes"))
    if is_paused:
        dash_state.set_bot_status("PAUSED")
    else:
        dash_state.set_bot_status("RUNNING")
    dash_state.strategy_name = config.strategy.strategy_name
    dash_state.prime_hours_only = getattr(config.strategy, "prime_hours_only", True)

    if reset:
        log.info("Reset requested: wiping trade log and reinitializing capital ledger at $%.2f starting equity", starting_equity_usd)
        ledger.clear_all_trades()
        capital_mgr.reset(starting_equity_usd=starting_equity_usd)
        dash_state.add_log("CONTROL", f"Reset complete: cleared historical trade log, starting equity set to ${starting_equity_usd:.2f} (PnL $0.00)")

    log.info(
        "%s ACTIVE | strategy=%s initial_stake=$%.2f reversal_stake=$%.2f latency=%dms paused=%s demo=%s",
        mode_label,
        config.strategy.strategy_name,
        config.strategy.initial_stake_usd,
        config.strategy.reversal_stake_usd,
        config.strategy.latency_ms,
        is_paused,
        demo,
    )

    if is_live:
        from polybot.execution.live import LiveExecutionEngine, create_clob_client
        clob_client = None
        try:
            clob_client = create_clob_client(config)
            log.info("Live Polymarket CLOB client initialized and authenticated")
        except Exception as e:
            log.error("Failed to initialize Live CLOB client: %s", e)
        engine = LiveExecutionEngine(config=config, clob_client=clob_client, ledger=ledger)
        # Fetch live on-chain/CLOB wallet balance immediately on startup
        live_wallet_bal = engine.get_wallet_balance(force_refresh=True)
        env_start = os.getenv("STARTING_EQUITY_USD")
        if live_wallet_bal <= 0.0 and env_start:
            live_wallet_bal = float(env_start)
        capital_mgr.sync_live_wallet_balance(live_wallet_bal)
        log.info("Live wallet balance linked to CapitalManager on startup: $%.2f", live_wallet_bal)
    else:
        engine = PaperExecutionEngine(config.strategy, ledger=ledger)
    client = PolymarketClient(config.feeds.gamma_rest_url, config.feeds.clob_rest_url)
    health = FeedHealthTracker(config.feeds.max_feed_staleness_sec, max_divergence_pct=config.feeds.max_divergence_pct)
    risk = RiskManager(config.risk)
    sizer = PositionSizer(config.risk, default_stake_usd=config.strategy.stake_usd)
    session_mgr = MarketSessionManager(
        window_duration_sec=300.0,
        waiting_period_sec=config.strategy.reversal_window_start_sec
    )

    # Check for Option B reserve activation upon bot startup/restart (Roadmap Phase 6)
    today = ist_today_str()
    activated, released_usd, _ = capital_mgr.check_and_activate_next_day_reserve(today)
    if activated:
        dash_state.add_log("RESERVE", f"Option B Reserve Release on startup: +${released_usd:.2f} active funds released for {today}")
        log.info("Option B Reserve Release on startup: +$%.2f into active capital", released_usd)

    # Initialize Capital Reconciler & run startup reconciliation (Roadmap Phase 7)
    reconciler = CapitalReconciler(capital_manager=capital_mgr, engine=engine)
    recon_report = reconciler.reconcile(trading_day=today)
    if not recon_report.is_valid:
        dash_state.add_log("RECON_ERR", f"Reconciliation discrepancy on startup: {recon_report.message}")
        log.error("Capital reconciliation discrepancy on startup: %s", recon_report.message)
    else:
        dash_state.add_log("RECON", f"Startup capital reconciliation verified: Total Equity=${capital_mgr.state.total_equity:.2f}")
        log.info("Startup capital reconciliation verified: Total Equity=$%.2f", capital_mgr.state.total_equity)

    # Populate enriched initial capital state in dashboard
    init_b, rev_b = capital_mgr.get_trade_budgets(today)
    dash_cap = capital_mgr.state.to_dict()
    dash_cap.update({
        "current_initial_budget": init_b,
        "current_reversal_budget": rev_b,
        "spendable_active_funds": capital_mgr.state.active_capital,
        "active_funds_reserved": 0.0,
        "protected_funds": capital_mgr.state.protected_capital,
        "unsettled_funds": 0.0,
    })
    dash_state.update_capital(dash_cap)

    dash_server = None
    if config.dashboard.enabled:
        auth_mgr = None
        if config.dashboard.auth_enabled:
            from polybot.dashboard.auth import AuthManager
            auth_mgr = AuthManager(
                username=config.dashboard.auth_username,
                password_hash=config.dashboard.auth_password_hash,
                pin_hash=config.dashboard.auth_pin_hash,
                session_secret=config.dashboard.auth_session_secret,
                enabled=True,
            )
        dash_server = DashboardServer(
            dash_state,
            ledger,
            capital_mgr=capital_mgr,
            reconciler=reconciler,
            auth_mgr=auth_mgr,
            host=config.dashboard.host,
            port=config.dashboard.port,
        )
        await dash_server.start()

    # STRICT PRODUCTION RULE: Live mode is strictly isolated to BTC!
    # Paper Mode enables full 6-market multi-asset parallel execution.
    monitored_assets: List[str] = ["btc"] if is_live else ["btc", "eth", "sol", "doge", "xrp", "bnb"]

    strategies: Dict[str, Any] = {}
    for a in monitored_assets:
        if config.strategy.strategy_name == "legacy":
            strategies[a] = LegacyStrategy(config.strategy)
        elif config.strategy.strategy_name == "reversal":
            strategies[a] = ReversalStrategy(config.strategy)
        else:
            strategies[a] = EdgeStrategy(config.strategy)
    strat = strategies["btc"]

    from polybot.multi_market import MultiMarketOrchestrator
    from polybot.capital.cell import CellStatus
    multi_orch = MultiMarketOrchestrator(assets=monitored_assets)
    dash_state.update_multi_market(multi_orch.get_matrix_snapshot())

    rtds = None
    binance = None
    clob = CLOBOrderBookFeed(ws_url=config.feeds.clob_ws_url, rest_client=client)

    if demo:
        log.info("DEMO MODE ENABLED: Simulating 5-minute market cycles and probability swings for live UI workstation testing.")
        dash_state.add_log("INFO", f"Demo simulation active across {len(monitored_assets)} markets: {', '.join(monitored_assets).upper()}")
        clob.track_multi_tokens({a: {"Up": f"demo-{a}-up", "Down": f"demo-{a}-down"} for a in monitored_assets})
    else:
        rtds = RTDSFeed(
            ws_url=config.feeds.rtds_ws_url,
            ping_interval_sec=config.feeds.rtds_ping_interval_sec,
            on_tick=lambda t: health.record_tick("chainlink", t["price"], t["timestamp_ms"]),
        )

        def handle_binance_multiplex_tick(t: Dict[str, Any]):
            asset = t.get("asset", "btc")
            if asset == "btc":
                health.record_tick("binance", t["price"], t["timestamp_ms"])
            multi_orch.on_tick(t)

        binance = BinanceFeed(
            symbols=["btcusdt", "ethusdt", "solusdt", "dogeusdt", "xrpusdt", "bnbusdt"],
            on_tick=handle_binance_multiplex_tick,
        )
        await rtds.start()
        await binance.start()
        await clob.start()

    current_window_start: Optional[int] = None
    current_tokens: Optional[Dict[str, str]] = None
    discovered_markets: Dict[str, Dict[str, Any]] = {}
    discovered_tokens: Dict[str, Dict[str, str]] = {}
    last_discovery_attempt_ts = 0.0
    open_trades_to_settle: List[Dict[str, Any]] = []
    # is_paused already determined above from config/args/env
    last_rest_poll_ts = 0.0
    last_balance_poll_ts = 0.0

    def handle_order_rejected(rej_order: Order, reason: str):
        rej_asset = rej_order.slug.split("-")[0].lower() if "-" in rej_order.slug else "btc"
        s = strategies.get(rej_asset, strat)
        if hasattr(s, "on_order_rejected"):
            s.on_order_rejected(rej_order.signal_type, cooldown_sec=1.0, current_ts=time.time())
        if not is_live:
            multi_orch.arbiter.release(rej_asset)
        if hasattr(risk, "record_trade_rejected"):
            risk.record_trade_rejected(rej_order.usd_amount)
        nonlocal open_trades_to_settle
        open_trades_to_settle = [
            it for it in open_trades_to_settle
            if it.get("order_id") != rej_order.order_id
        ]
        dash_state.add_log("REJECTED", f"{rej_order.signal_type} fill rejected: {reason}")
        log.warning("[ORDER_REJECTED_CALLBACK] %s fill rejected (%s) - state unlatched", rej_order.signal_type, reason)

    engine.on_order_rejected = handle_order_rejected

    if is_paused:
        dash_state.add_log("CONTROL", f"{mode_label} initialized in PAUSED state (awaiting wallet funding)")
        log.info("%s initialized in PAUSED state (awaiting wallet funding)", mode_label)
    else:
        dash_state.add_log("INFO", f"Bot initialized with {config.strategy.strategy_name} strategy")

    try:
        while True:
            # Handle UI controls
            action = dash_state.pop_requested_action()
            if action == "PAUSE":
                is_paused = True
                dash_state.set_bot_status("PAUSED")
                dash_state.add_log("CONTROL", f"{mode_label} PAUSED via dashboard control")
                log.info("%s PAUSED via dashboard control", mode_label)
            elif action in ("RESUME", "START"):
                is_paused = False
                dash_state.set_bot_status("RUNNING")
                dash_state.add_log("CONTROL", f"{mode_label} RESUMED via dashboard control")
                log.info("%s RESUMED via dashboard control", mode_label)
            elif action == "STOP":
                dash_state.set_bot_status("STOPPED")
                dash_state.add_log("CONTROL", f"{mode_label} STOPPED via dashboard control")
                log.info("%s STOPPED via dashboard control", mode_label)
                break

            if risk.is_kill_switch_active():
                dash_state.set_bot_status("STOPPED")
                log.warning("Kill switch file '%s' detected! Halting paper trading.", config.risk.kill_switch_file)
                break

            now = time.time()

            # Dynamic live wallet balance synchronization (sub-second polling & scaling)
            if is_live and hasattr(engine, "get_wallet_balance") and (now - last_balance_poll_ts >= 1.0):
                last_balance_poll_ts = now
                current_bal = engine.get_wallet_balance()
                env_start = os.getenv("STARTING_EQUITY_USD")
                if current_bal <= 0.0 and env_start:
                    current_bal = float(env_start)
                if abs(current_bal - capital_mgr.state.total_equity) >= 0.009:
                    old_tot = capital_mgr.state.total_equity
                    capital_mgr.sync_live_wallet_balance(current_bal, auto_rebalance=True)
                    open_exp = engine.get_open_exposure()
                    init_b, rev_b = capital_mgr.get_trade_budgets()
                    dash_cap = capital_mgr.state.to_dict()
                    dash_cap.update({
                        "current_initial_budget": init_b,
                        "current_reversal_budget": rev_b,
                        "open_exposure": round(open_exp, 2),
                        "spendable_active_funds": max(0.0, round(capital_mgr.state.active_capital - open_exp, 2)),
                        "active_funds_reserved": round(open_exp, 2),
                        "protected_funds": capital_mgr.state.protected_capital,
                        "unsettled_funds": round(open_exp, 2),
                    })
                    dash_state.update_capital(dash_cap)
                    dash_state.add_log("BALANCE", f"Live wallet rebalanced: Total ${old_tot:.2f} -> ${current_bal:.2f} (Active: ${capital_mgr.state.active_capital:.2f}, Prot: ${capital_mgr.state.protected_capital:.2f}, Init: ${init_b}, Rev: ${rev_b})")
                    log.info("[LIVE_BALANCE_SYNC] Wallet rebalanced: Total $%.2f -> $%.2f (Active: $%.2f, Prot: $%.2f, Init: $%d, Rev: $%d)", old_tot, current_bal, capital_mgr.state.active_capital, capital_mgr.state.protected_capital, init_b, rev_b)

            time_state = session_mgr.sync(now)
            window_start = int(time_state.window_start_ts)
            current_window_idx = window_start // 300
            current_slug = f"btc-updown-5m-{window_start}"

            # Window rollover
            if current_window_start != window_start:
                current_window_start = window_start
                current_tokens = None
                discovered_markets.clear()
                discovered_tokens.clear()
                for a in monitored_assets:
                    cur_k = multi_orch.trackers[a].strike_k or multi_orch.trackers[a].spot_p
                    if a == "btc" and health.chainlink.last_price:
                        cur_k = health.chainlink.last_price
                    strategies[a].reset_window(window_start, cur_k)

            # Market discovery (demo or live)
            if demo:
                if not discovered_tokens:
                    for a in monitored_assets:
                        discovered_tokens[a] = {"Up": f"demo-{a}-up", "Down": f"demo-{a}-down"}
                        multi_orch.trackers[a].update_market_info({
                            "slug": f"{a}-updown-5m-{window_start}",
                            "tokens": discovered_tokens[a],
                        })
                        base_k = 68500.0 if a == "btc" else (multi_orch.trackers[a].strike_k or 100.0)
                        strategies[a].reset_window(window_start, base_k)
                        dash_state.add_log("INFO", f"[{a.upper()}] Demo rollover into window {window_start}")
                    current_tokens = discovered_tokens.get("btc")
                    clob.track_multi_tokens(discovered_tokens)
                    log.info("Demo rollover window %d for assets: %s", window_start, list(discovered_tokens.keys()))
            else:
                if is_live:
                    # STRICT LIVE TRADING: Only discover BTC
                    if not current_tokens and (now - last_discovery_attempt_ts > 2.0):
                        last_discovery_attempt_ts = now
                        try:
                            m_info = await asyncio.wait_for(
                                asyncio.to_thread(client.get_market, window_start, asset="btc"),
                                timeout=2.0
                            )
                        except Exception as exc:
                            m_info = None
                            log.debug("Market discovery timed out for %s: %s", current_slug, exc)

                        if m_info and m_info.get("tokens"):
                            current_tokens = m_info["tokens"]
                            discovered_tokens["btc"] = current_tokens
                            discovered_markets["btc"] = m_info
                            clob.track_tokens(current_tokens)
                            K_val = health.chainlink.last_price
                            strat.reset_window(window_start, K_val)
                            dash_state.add_log("INFO", f"Discovered market {current_slug} tokens")
                            log.info("Market %d discovered: strike K=%s, tokens=%s", window_start, f"${K_val:.2f}" if K_val else "pending", current_tokens)
                            try:
                                await asyncio.wait_for(asyncio.to_thread(clob.poll_rest_fallback), timeout=2.0)
                            except Exception:
                                pass
                        else:
                            dash_state.add_log("WARN", f"Market lookup pending for {current_slug} (retrying in 2s)")
                else:
                    # PAPER MODE: Multi-Market Discovery across all monitored assets
                    missing_assets = [a for a in monitored_assets if a not in discovered_markets]
                    if missing_assets and (now - last_discovery_attempt_ts > 2.0):
                        last_discovery_attempt_ts = now
                        try:
                            m_dict = await asyncio.wait_for(
                                asyncio.to_thread(client.get_multi_markets, window_start, missing_assets),
                                timeout=3.0
                            )
                        except Exception as exc:
                            m_dict = {}
                            log.debug("Multi-market discovery timeout for %s: %s", missing_assets, exc)

                        if m_dict:
                            updated_any = False
                            for a, m_info in m_dict.items():
                                if m_info and m_info.get("tokens"):
                                    discovered_markets[a] = m_info
                                    discovered_tokens[a] = m_info["tokens"]
                                    multi_orch.trackers[a].update_market_info(m_info)
                                    cur_spot = multi_orch.trackers[a].spot_p or (health.chainlink.last_price if a == "btc" else None)
                                    if cur_spot and cur_spot > 0 and multi_orch.trackers[a].strike_k is None:
                                        multi_orch.trackers[a].strike_k = cur_spot
                                    strategies[a].reset_window(window_start, multi_orch.trackers[a].strike_k)
                                    dash_state.add_log("INFO", f"[{a.upper()}] Discovered market {m_info['slug']} tokens")
                                    log.info("[%s] Market %d discovered: strike K=%s, tokens=%s",
                                             a.upper(), window_start,
                                             f"${multi_orch.trackers[a].strike_k:.2f}" if multi_orch.trackers[a].strike_k else "pending",
                                             m_info["tokens"])
                                    updated_any = True

                            if updated_any:
                                current_tokens = discovered_tokens.get("btc")
                                clob.track_multi_tokens(discovered_tokens)
                                try:
                                    await asyncio.wait_for(asyncio.to_thread(clob.poll_rest_fallback), timeout=2.0)
                                except Exception:
                                    pass

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

                for a in monitored_assets:
                    tr = multi_orch.trackers[a]
                    up_p_a = tr.up_prob / 100.0
                    down_p_a = tr.down_prob / 100.0
                    u_tid = discovered_tokens.get(a, {}).get("Up", f"demo-{a}-up")
                    d_tid = discovered_tokens.get(a, {}).get("Down", f"demo-{a}-down")
                    if u_tid in clob.books:
                        clob.books[u_tid].apply_snapshot(
                            [{"price": round(up_p_a - 0.01, 3), "size": 500}],
                            [{"price": round(up_p_a, 3), "size": 500}]
                        )
                    if d_tid in clob.books:
                        clob.books[d_tid].apply_snapshot(
                            [{"price": round(down_p_a - 0.01, 3), "size": 500}],
                            [{"price": round(down_p_a, 3), "size": 500}]
                        )

                # Update 6-Market Matrix Tracker in Demo Mode
                multi_orch.trackers["btc"].strike_k = base_k
                multi_orch.trackers["btc"].update_spot_price(cur_btc)
                multi_orch.trackers["btc"].update_probabilities(up_p * 100.0, down_p * 100.0)

                # ETH (5m)
                multi_orch.trackers["eth"].strike_k = 2860.50
                eth_spot = 2860.50 + 12.0 * math.sin((elapsed + 20) / 14.0)
                eth_up = 52.0 + 38.0 * math.sin((elapsed + 15) / 22.0)
                multi_orch.trackers["eth"].update_spot_price(eth_spot)
                multi_orch.trackers["eth"].update_probabilities(max(5.0, min(95.0, eth_up)), 100.0 - max(5.0, min(95.0, eth_up)))

                # SOL (5m)
                multi_orch.trackers["sol"].strike_k = 194.80
                sol_spot = 194.80 + 2.5 * math.sin((elapsed + 50) / 16.0)
                sol_up = 48.0 + 30.0 * math.sin((elapsed + 60) / 25.0)
                multi_orch.trackers["sol"].update_spot_price(sol_spot)
                multi_orch.trackers["sol"].update_probabilities(max(5.0, min(95.0, sol_up)), 100.0 - max(5.0, min(95.0, sol_up)))

                # DOGE (5m)
                multi_orch.trackers["doge"].strike_k = 0.19820
                doge_spot = 0.19820 + 0.002 * math.sin(elapsed / 18.0)
                doge_up = 51.0 + 20.0 * math.sin(elapsed / 30.0)
                multi_orch.trackers["doge"].update_spot_price(doge_spot)
                multi_orch.trackers["doge"].update_probabilities(max(5.0, min(95.0, doge_up)), 100.0 - max(5.0, min(95.0, doge_up)))

                # XRP (5m)
                multi_orch.trackers["xrp"].strike_k = 0.58240
                xrp_spot = 0.58240 + 0.007 * math.sin((elapsed + 45) / 16.0)
                xrp_up = 49.0 + 25.0 * math.sin((elapsed + 10) / 20.0)
                multi_orch.trackers["xrp"].update_spot_price(xrp_spot)
                multi_orch.trackers["xrp"].update_probabilities(max(5.0, min(95.0, xrp_up)), 100.0 - max(5.0, min(95.0, xrp_up)))

                # BNB (5m)
                multi_orch.trackers["bnb"].strike_k = 618.40
                bnb_spot = 618.40 + 3.5 * math.sin((elapsed + 80) / 15.0)
                bnb_up = 50.0 + 15.0 * math.sin((elapsed + 35) / 28.0)
                multi_orch.trackers["bnb"].update_spot_price(bnb_spot)
                multi_orch.trackers["bnb"].update_probabilities(max(5.0, min(95.0, bnb_up)), 100.0 - max(5.0, min(95.0, bnb_up)))

            # Process pending latency fills (passing side_books mapping)
            new_fills = engine.process_pending_orders(now, clob.side_books)
            for f in new_fills:
                dash_state.add_log(
                    "BUY" if f.action == "BUY" else "SELL",
                    f"Paper fill: {f.signal_type} {f.side} {f.shares:.2f}sh @ ${f.price:.3f} (${f.cost_usd:.2f})"
                )
                if f.action == "SELL":
                    open_trades_to_settle = [it for it in open_trades_to_settle if it.get("slug") != f.slug]
                    closed_pos = next((p for p in engine.settled_positions if p.order_id == f.linked_order_id or p.slug == f.slug), None)
                    if closed_pos:
                        risk.record_trade_settled(closed_pos.realized_pnl_usd, closed_pos.total_cost_usd, current_window_idx)
                        settle_id = f"settle-early-{f.slug}-{closed_pos.order_id}"
                        try:
                            capital_mgr.process_settlement(
                                settlement_id=settle_id,
                                net_pnl_usd=closed_pos.realized_pnl_usd,
                                fee_usd=closed_pos.total_fees_usd,
                                trading_day=ist_today_str(),
                                signal_type=closed_pos.signal_type,
                            )
                            log.info("[EARLY_EXIT_SETTLED] %s net_pnl=$%+.2f payout=$%.2f",
                                     settle_id, closed_pos.realized_pnl_usd, closed_pos.payout_usd)
                        except Exception as e:
                            log.error("Failed to process early exit settlement for %s: %s", settle_id, e)

                        open_exp = engine.get_open_exposure()
                        init_b, rev_b = capital_mgr.get_trade_budgets(ist_today_str())
                        dash_cap = capital_mgr.state.to_dict()
                        dash_cap.update({
                            "current_initial_budget": init_b,
                            "current_reversal_budget": rev_b,
                            "open_exposure": round(open_exp, 2),
                            "spendable_active_funds": max(0.0, round(capital_mgr.state.active_capital - open_exp, 2)),
                            "active_funds_reserved": round(open_exp, 2),
                            "protected_funds": capital_mgr.state.protected_capital,
                            "unsettled_funds": round(open_exp, 2),
                        })
                        dash_state.update_capital(dash_cap)

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
            if not is_live:
                for a in monitored_assets:
                    up_a = clob.get_effective_ask("Up", asset=a)
                    down_a = clob.get_effective_ask("Down", asset=a)
                    up_b_a = clob.get_effective_bid("Up", asset=a)
                    down_b_a = clob.get_effective_bid("Down", asset=a)
                    if not demo and up_a is not None and down_a is not None:
                        multi_orch.trackers[a].update_book(up_a, down_a, up_b_a, down_b_a)
                        multi_orch.trackers[a].update_probabilities(up_a * 100.0, down_a * 100.0)

            up_ask = clob.get_effective_ask("Up", asset="btc") or clob.get_effective_ask("Up")
            down_ask = clob.get_effective_ask("Down", asset="btc") or clob.get_effective_ask("Down")
            up_bid = clob.get_effective_bid("Up", asset="btc") or clob.get_effective_bid("Up")
            down_bid = clob.get_effective_bid("Down", asset="btc") or clob.get_effective_bid("Down")

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
                        up_ask = clob.get_effective_ask("Up", asset="btc") or clob.get_effective_ask("Up")
                        down_ask = clob.get_effective_ask("Down", asset="btc") or clob.get_effective_ask("Down")
                        up_bid = clob.get_effective_bid("Up", asset="btc") or clob.get_effective_bid("Up")
                        down_bid = clob.get_effective_bid("Down", asset="btc") or clob.get_effective_bid("Down")
                    except Exception:
                        pass

            dash_state.update_probabilities(
                up_prob=up_ask,
                down_prob=down_ask,
                up_bid=up_bid,
                down_bid=down_bid,
                latency_ms=config.strategy.latency_ms,
            )
            dash_state.update_multi_market(multi_orch.get_matrix_snapshot())

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
                today = ist_today_str()
                # Check Option B Reserve Release for new trading day (Roadmap Phase 6)
                activated, released_usd, _ = capital_mgr.check_and_activate_next_day_reserve(today)
                if activated:
                    dash_state.add_log("RESERVE", f"Option B Reserve Release: +${released_usd:.2f} active funds released for {today}")
                    dash_state.update_capital(capital_mgr.state.to_dict())
                    log.info("Option B Reserve Release executed for %s: +$%.2f into active capital", today, released_usd)

                if is_live:
                    # STRICT LIVE TRADING: ONLY BTC!
                    cur_price = health.chainlink.last_price or 0.0
                    if isinstance(strat, ReversalStrategy):
                        init_b, rev_b = budget_calc.get_budgets(capital_mgr.state.active_capital)
                        open_exp = engine.get_open_exposure()
                        if strat.initial_order_placed or open_exp > 0:
                            exec_rev = budget_calc.get_executable_reversal_budget(
                                capital_mgr.state.active_capital,
                                open_exposure_usd=open_exp,
                            )
                            if exec_rev >= budget_calc.min_order_usd:
                                rev_b = exec_rev
                        strat.set_dynamic_budgets(init_b, rev_b)

                    sig = strat.on_tick(cur_price, now, clob.side_books, health)
                    if sig:
                        if sig.action == "SELL":
                            tok_id = current_tokens.get(sig.side) if current_tokens else None
                            order = Order(
                                order_id=f"live-{int(now * 1000)}",
                                slug=sig.slug,
                                side=sig.side,
                                action=sig.action,
                                order_type=sig.order_type,
                                price=sig.price,
                                usd_amount=sig.cost_usd,
                                shares=sig.shares,
                                created_at_ts=now,
                                latency_ms=config.strategy.latency_ms,
                                signal_type=sig.signal_type,
                                linked_order_id=sig.linked_order_id,
                                trigger_prob=sig.trigger_prob,
                                counterpart_prob=sig.counterpart_prob,
                                data_receipt_ts=sig.data_receipt_ts,
                                token_id=tok_id,
                            )
                            engine.submit_order(order)
                            dash_state.add_log(
                                "EARLY_EXIT",
                                f"Early exit sell placed: {order.order_id} ({sig.side} {sig.shares:.2f} shs @ ${sig.price:.3f})"
                            )
                            continue

                        stake_usd = sig.cost_usd
                        risk.sync_open_exposure(engine.get_open_exposure())

                        today = ist_today_str()
                        if capital_mgr.is_daily_shutdown_active(today):
                            if hasattr(strat, "on_order_rejected"):
                                strat.on_order_rejected(sig.signal_type, cooldown_sec=3.0, current_ts=now)
                            log.warning("Order blocked: Daily shutdown active for %s (%s)", today, capital_mgr.state.daily_shutdown_reason)
                            dash_state.add_log("SHUTDOWN", f"Daily shutdown active: {capital_mgr.state.daily_shutdown_reason}")
                            continue

                        if capital_mgr.state.active_shortfall_cents > 0:
                            if hasattr(strat, "on_order_rejected"):
                                strat.on_order_rejected(sig.signal_type, cooldown_sec=3.0, current_ts=now)
                            log.warning("Order blocked: Active shortfall ($%.2f) exists. Trading halted.", capital_mgr.state.active_shortfall)
                            dash_state.add_log("WARNING", f"Shortfall halt: ${capital_mgr.state.active_shortfall:.2f}")
                            continue

                        b_val = budget_calc.validate_order_budget(
                            signal_type=sig.signal_type,
                            active_capital_usd=capital_mgr.state.active_capital,
                            open_exposure_usd=engine.get_open_exposure(),
                            override_budget_usd=sig.cost_usd,
                            allow_adaptive_clamping=True,
                        )
                        if not b_val.allowed:
                            if hasattr(strat, "on_order_rejected"):
                                strat.on_order_rejected(sig.signal_type, cooldown_sec=3.0, current_ts=now)
                            log.warning("[ORDER_BUDGET_REJECTED] %s: %s", sig.signal_type, b_val.reason)
                            dash_state.add_log("REJECTED", f"Order budget blocked ({sig.signal_type}): {b_val.reason}")
                            continue

                        if b_val.approved_budget_usd > 0 and b_val.approved_budget_usd < stake_usd:
                            scale = b_val.approved_budget_usd / stake_usd
                            stake_usd = b_val.approved_budget_usd
                            sig.shares = round(sig.shares * scale, 4)
                            sig.cost_usd = stake_usd

                        allowed, reason = risk.check_trade_allowed(
                            stake_usd, current_window_idx, active_capital_usd=capital_mgr.state.active_capital
                        )
                        if allowed:
                            tok_id = current_tokens.get(sig.side) if current_tokens else None
                            order = Order(
                                order_id=f"live-{int(now * 1000)}",
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
                                token_id=tok_id,
                            )
                            engine.submit_order(order)
                            risk.record_trade_placed(stake_usd)
                            open_trades_to_settle.append({
                                "slug": sig.slug,
                                "order_id": order.order_id,
                                "cost": stake_usd,
                                "window_idx": current_window_idx,
                                "window_end": (current_window_idx + 1) * 300,
                                "last_checked_ts": 0.0,
                            })
                            dash_state.add_log(
                                "SIGNAL" if sig.signal_type == "INITIAL" else "REVERSAL",
                                f"Signal triggered: {sig.signal_type} {sig.side} (${stake_usd:.2f}) prob={sig.trigger_prob:.3f}"
                            )
                            dash_state.add_log(
                                "ORDER",
                                f"Order placed: {order.order_id} ({sig.signal_type} {sig.side} ${stake_usd:.2f} limit=${sig.price:.3f})"
                            )
                        else:
                            if hasattr(strat, "on_order_rejected"):
                                strat.on_order_rejected(sig.signal_type, cooldown_sec=3.0, current_ts=now)
                            log.warning("Trade rejected by Risk Manager: %s", reason)
                            dash_state.add_log("REJECTED", f"Risk check rejected ({sig.signal_type}): {reason}")
                else:
                    # PAPER TRADING: Multi-Market Parallel Execution across all 6 pairs
                    for a in monitored_assets:
                        strat_a = strategies[a]
                        cell_a = multi_orch.cell_manager.get_cell(a)
                        if cell_a.status == CellStatus.HALTED_DEPLETED:
                            continue

                        init_b = cell_a.get_initial_budget()
                        rev_b = cell_a.get_reversal_budget()
                        if hasattr(strat_a, "set_dynamic_budgets"):
                            strat_a.set_dynamic_budgets(init_b, rev_b)

                        spot_a = multi_orch.trackers[a].spot_p or (health.chainlink.last_price if a == "btc" else 0.0)
                        books_a = clob.get_asset_side_books(a)
                        if not books_a and a == "btc":
                            books_a = clob.side_books

                        sig = strat_a.on_tick(spot_a, now, books_a, health)
                        if sig:
                            tok_id = discovered_tokens.get(a, {}).get(sig.side)
                            if sig.action == "SELL":
                                # Early exit sell
                                order = Order(
                                    order_id=f"paper-{a}-{int(now * 1000)}",
                                    slug=sig.slug,
                                    side=sig.side,
                                    action=sig.action,
                                    order_type=sig.order_type,
                                    price=sig.price,
                                    usd_amount=sig.cost_usd,
                                    shares=sig.shares,
                                    created_at_ts=now,
                                    latency_ms=config.strategy.latency_ms,
                                    signal_type=sig.signal_type,
                                    linked_order_id=sig.linked_order_id,
                                    trigger_prob=sig.trigger_prob,
                                    counterpart_prob=sig.counterpart_prob,
                                    data_receipt_ts=sig.data_receipt_ts,
                                    token_id=tok_id,
                                )
                                engine.submit_order(order)
                                dash_state.add_log(
                                    "EARLY_EXIT",
                                    f"[{a.upper()}] Early exit sell placed: {order.order_id} ({sig.side} {sig.shares:.2f} shs @ ${sig.price:.3f})"
                                )
                                continue

                            fleet_equity = multi_orch.cell_manager.total_fleet_equity_usd
                            if not multi_orch.arbiter.can_acquire(a, fleet_equity):
                                dash_state.add_log(
                                    "ARBITER",
                                    f"[{a.upper()}] Concurrency slot denied for {sig.signal_type} {sig.side} (Max slots: {multi_orch.arbiter.get_max_slots(fleet_equity)})"
                                )
                                continue

                            multi_orch.arbiter.acquire(a, sig.signal_type, sig.cost_usd, fleet_equity)
                            order = Order(
                                order_id=f"paper-{a}-{int(now * 1000)}",
                                slug=sig.slug,
                                side=sig.side,
                                action=sig.action,
                                order_type=sig.order_type,
                                price=sig.price,
                                usd_amount=sig.cost_usd,
                                shares=sig.shares,
                                created_at_ts=now,
                                latency_ms=config.strategy.latency_ms,
                                signal_type=sig.signal_type,
                                linked_order_id=sig.linked_order_id,
                                trigger_prob=sig.trigger_prob,
                                counterpart_prob=sig.counterpart_prob,
                                data_receipt_ts=sig.data_receipt_ts,
                                token_id=tok_id,
                            )
                            engine.submit_order(order)
                            risk.record_trade_placed(sig.cost_usd)
                            open_trades_to_settle.append({
                                "slug": sig.slug,
                                "order_id": order.order_id,
                                "asset": a,
                                "cost": sig.cost_usd,
                                "window_idx": current_window_idx,
                                "window_end": (current_window_idx + 1) * 300,
                                "last_checked_ts": 0.0,
                            })
                            dash_state.add_log(
                                "SIGNAL" if sig.signal_type == "INITIAL" else "REVERSAL",
                                f"[{a.upper()}] Signal triggered: {sig.signal_type} {sig.side} (${sig.cost_usd:.2f}) prob={sig.trigger_prob:.3f}"
                            )
                            dash_state.add_log(
                                "ORDER",
                                f"[{a.upper()}] Order placed: {order.order_id} ({sig.signal_type} {sig.side} ${sig.cost_usd:.2f} limit=${sig.price:.3f})"
                            )

            # Check settlements for concluded windows
            pending_slugs = {item["slug"] for item in open_trades_to_settle if now >= item.get("window_end", 0)}
            for slug in pending_slugs:
                slug_asset = slug.split("-")[0].lower() if "-" in slug else "btc"
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

                        if is_live:
                            # Capital Ledger Settlement Hook (Roadmap Phase 4 & 5) - LIVE BTC ONLY
                            settle_id = f"settle-{slug}-{settled_pos.order_id or settled_pos.position_id or int(now * 1000)}"
                            try:
                                capital_mgr.process_settlement(
                                    settlement_id=settle_id,
                                    net_pnl_usd=settled_pos.realized_pnl_usd,
                                    fee_usd=settled_pos.total_fees_usd,
                                    trading_day=ist_today_str(),
                                    signal_type=settled_pos.signal_type,
                                )
                                log.info("[SETTLEMENT_PROCESSED] settle_id=%s net_pnl=$%+.2f fee=$%.2f",
                                         settle_id, settled_pos.realized_pnl_usd, settled_pos.total_fees_usd)
                            except Exception as e:
                                log.error("Failed to process capital settlement for %s: %s", settle_id, e)

                            # If daily shutdown triggered, safely cancel pending orders
                            if capital_mgr.is_daily_shutdown_active():
                                cancelled_cnt = engine.cancel_pending_orders()
                                if cancelled_cnt > 0:
                                    log.info("Cancelled %d unfilled pending orders due to daily shutdown", cancelled_cnt)

                            open_exp = engine.get_open_exposure()
                            init_b, rev_b = capital_mgr.get_trade_budgets(ist_today_str())
                            dash_cap = capital_mgr.state.to_dict()
                            dash_cap.update({
                                "current_initial_budget": init_b,
                                "current_reversal_budget": rev_b,
                                "open_exposure": round(open_exp, 2),
                                "spendable_active_funds": max(0.0, round(capital_mgr.state.active_capital - open_exp, 2)),
                                "active_funds_reserved": round(open_exp, 2),
                                "protected_funds": capital_mgr.state.protected_capital,
                                "unsettled_funds": round(open_exp, 2),
                            })
                            dash_state.update_capital(dash_cap)
                            dash_state.add_log(
                                "SETTLED",
                                f"Settled {slug}: position={settled_pos.side} ({settled_pos.signal_type}) winner={winner} | "
                                f"PnL=${settled_pos.realized_pnl_usd:+.2f} | Active=${capital_mgr.state.active_capital:.2f} "
                                f"Prot=${capital_mgr.state.protected_capital:.2f} Total=${capital_mgr.state.total_equity:.2f}"
                            )
                        else:
                            # PAPER MODE: Route to compartmentalized MarketCell and check $100 harvest
                            cell = multi_orch.cell_manager.get_cell(slug_asset)
                            is_rev = settled_pos.signal_type == "REVERSAL"
                            cell.record_trade_result(settled_pos.realized_pnl_usd, is_reversal=is_rev)

                            harvest = multi_orch.cell_manager.check_and_trigger_harvest(slug_asset)
                            if harvest:
                                dash_state.add_log(
                                    "HARVEST",
                                    f"[{slug_asset.upper()}] $100 TARGET GRADUATED! Self-seeded $12.00, Revived {harvest.halted_cells_revived} cells (${harvest.halted_cells_revived * 12:.2f}), Swept ${harvest.amount_swept_usd:.2f} to Binance Cold Storage!"
                                )

                            multi_orch.arbiter.release(slug_asset)

                            settle_id = f"settle-{slug}-{settled_pos.order_id or settled_pos.position_id or int(now * 1000)}"
                            try:
                                capital_mgr.process_settlement(
                                    settlement_id=settle_id,
                                    net_pnl_usd=settled_pos.realized_pnl_usd,
                                    fee_usd=settled_pos.total_fees_usd,
                                    trading_day=ist_today_str(),
                                    signal_type=settled_pos.signal_type,
                                )
                            except Exception:
                                pass

                            dash_state.add_log(
                                "SETTLED",
                                f"[{slug_asset.upper()}] Settled {slug}: position={settled_pos.side} ({settled_pos.signal_type}) winner={winner} | "
                                f"PnL=${settled_pos.realized_pnl_usd:+.2f} | Cell Equity=${cell.capital_usd:.2f} | Fleet Total=${multi_orch.cell_manager.total_fleet_equity_usd:.2f}"
                            )

                        log.info(
                            "PAPER POSITION SETTLED %s %s (%s) | PnL=$%+.2f | Total PnL=$%+.2f | Capital: Active=$%.2f Prot=$%.2f Total=$%.2f",
                            slug,
                            settled_pos.side,
                            settled_pos.signal_type,
                            settled_pos.realized_pnl_usd,
                            engine.get_total_realized_pnl(),
                            capital_mgr.state.active_capital,
                            capital_mgr.state.protected_capital,
                            capital_mgr.state.total_equity,
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
    from polybot.capital import CapitalManager, CapitalReconciler

    ledger = TradeLedger(config.recorder.data_dir / "paper_ledger.db")
    capital_mgr = CapitalManager(
        db_path=config.recorder.data_dir / "paper_ledger.db",
        starting_equity_usd=11.0,
        sizing_mode=os.getenv("SIZING_MODE", "canonical"),
    )
    reconciler = CapitalReconciler(capital_manager=capital_mgr)
    state = DashboardState()
    state.set_bot_status("STANDALONE_VIEW")
    state.update_capital(capital_mgr.state.to_dict())
    auth_mgr = None
    if config.dashboard.auth_enabled:
        from polybot.dashboard.auth import AuthManager
        auth_mgr = AuthManager(
            username=config.dashboard.auth_username,
            password_hash=config.dashboard.auth_password_hash,
            pin_hash=config.dashboard.auth_pin_hash,
            session_secret=config.dashboard.auth_session_secret,
            enabled=True,
        )
    server = DashboardServer(
        state,
        ledger,
        capital_mgr=capital_mgr,
        reconciler=reconciler,
        auth_mgr=auth_mgr,
        host=config.dashboard.host,
        port=config.dashboard.port
    )
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
    paper_parser.add_argument("--reset", action="store_true", help="Reset all trades and capital state to starting balance")
    paper_parser.add_argument("--paused", action="store_true", help="Start bot in paused state awaiting manual resume")
    paper_parser.add_argument("--mode", choices=["paper", "live"], default=None, help="Execution mode override (paper or live)")

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
            asyncio.run(
                run_paper(
                    config,
                    demo=getattr(args, "demo", False),
                    reset=getattr(args, "reset", False),
                    paused=getattr(args, "paused", False),
                    mode=getattr(args, "mode", None),
                )
            )
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
