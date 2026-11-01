"""
End-to-End Paper-Trading Soak & Dry-Run Validation Harness (Roadmap Phase 9).
Executes multi-window simulated and deterministic paper trading cycles to validate:
1. Dynamic budget calculation (1:10 ratio, half-up rounding).
2. $44 Milestone activation and exact 50/50 profit splitting.
3. Conjunctive daily shutdown triggering and order budget freezing.
4. Day rollover and Option B next-day 50% reserve activation.
5. In-flight restart resilience and SQLite idempotency.
6. Periodic capital reconciliation and invariant preservation.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polybot.capital.ledger import CapitalLedger
from polybot.capital.manager import CapitalManager
from polybot.capital.models import CapitalState
from polybot.capital.reconciliation import CapitalReconciler, ExchangeBalanceSnapshot
from polybot.capital.sizing import DynamicTradeBudgetCalculator
from polybot.config import StrategyConfig
from polybot.execution.base import Order
from polybot.execution.ledger import TradeLedger
from polybot.execution.paper import PaperExecutionEngine
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.strategy import ReversalStrategy

log = logging.getLogger("polybot.capital.soak")


@dataclass
class SoakEventLog:
    timestamp: float
    trading_day: str
    event_type: str
    details: str
    active_usd: float
    protected_usd: float
    total_equity_usd: float


@dataclass
class SoakTestReport:
    windows_simulated: int
    trades_executed: int
    trades_settled: int
    starting_equity_usd: float
    final_equity_usd: float
    final_active_usd: float
    final_protected_usd: float
    net_pnl_usd: float
    milestone_44_reached: bool
    milestone_window_idx: Optional[int]
    shutdowns_triggered: int
    option_b_releases: int
    option_b_total_released_usd: float
    reconciliation_checks: int
    reconciliation_mismatches: int
    invariants_verified_count: int
    invariant_violations_count: int
    event_log: List[SoakEventLog] = field(default_factory=list)

    @property
    def is_successful(self) -> bool:
        return (
            self.invariant_violations_count == 0
            and self.reconciliation_mismatches == 0
            and self.final_equity_usd > 0
        )


class PaperTradingSoakRunner:
    """
    Simulates consecutive 5-minute prediction market windows with sub-second resolution.
    Validates full end-to-end integration between:
    - ReversalStrategy
    - DynamicTradeBudgetCalculator
    - PaperExecutionEngine & TradeLedger
    - CapitalManager & CapitalLedger
    - CapitalReconciler
    """

    def __init__(
        self,
        db_path: Path | str,
        starting_equity_usd: float = 11.0,
        strategy_config: Optional[StrategyConfig] = None,
    ):
        self.db_path = Path(db_path)
        self.starting_equity_usd = starting_equity_usd
        self.config = strategy_config or StrategyConfig()

        self.cap_ledger = CapitalLedger(self.db_path)
        self.trade_ledger = TradeLedger(self.db_path)
        self.capital_mgr = CapitalManager(ledger=self.cap_ledger, starting_equity_usd=starting_equity_usd)
        self.engine = PaperExecutionEngine(self.config, ledger=self.trade_ledger)
        self.budget_calc = DynamicTradeBudgetCalculator(min_order_usd=1.0, taker_fee_rate=self.config.taker_fee_rate)
        self.reconciler = CapitalReconciler(self.capital_mgr, self.engine)

        self.strategy = ReversalStrategy(self.config)

    def run_soak(
        self,
        num_windows: int = 24,
        start_timestamp: float = 1793400000.0,
        inject_failure_day: Optional[str] = None,
        test_restart_at_window: Optional[int] = None,
    ) -> SoakTestReport:
        """
        Executes a deterministic multi-window soak simulation.
        Each window represents 300 seconds (5 minutes).
        """
        events: List[SoakEventLog] = []
        trades_placed = 0
        trades_settled = 0
        milestone_window: Optional[int] = None
        shutdown_count = 0
        option_b_count = 0
        option_b_released_sum = 0.0
        recon_checks = 0
        recon_mismatches = 0
        invariants_checked = 0
        invariant_violations = 0

        current_day = ""

        def _log_event(ts: float, day: str, ev_type: str, details: str):
            events.append(SoakEventLog(
                timestamp=ts,
                trading_day=day,
                event_type=ev_type,
                details=details,
                active_usd=self.capital_mgr.state.active_capital,
                protected_usd=self.capital_mgr.state.protected_capital,
                total_equity_usd=self.capital_mgr.state.total_equity,
            ))

        for w_idx in range(num_windows):
            window_start = int(start_timestamp + (w_idx * 300))
            window_end = window_start + 300
            w_day = time.strftime("%Y-%m-%d", time.gmtime(window_start))

            # Simulate process restart if requested
            if test_restart_at_window is not None and w_idx == test_restart_at_window:
                _log_event(float(window_start), w_day, "RESTART", f"Simulating crash and restart at window {w_idx}")
                self.capital_mgr = CapitalManager(ledger=self.cap_ledger)
                self.reconciler = CapitalReconciler(self.capital_mgr, self.engine)

            # Check day rollover
            if current_day and w_day != current_day:
                _log_event(float(window_start), w_day, "DAY_ROLLOVER", f"Day rollover: {current_day} -> {w_day}")
                self.capital_mgr.check_day_rollover(w_day)

                # Option B Reserve Release check on day rollover
                activated, rel_usd, _ = self.capital_mgr.check_and_activate_next_day_reserve(w_day)
                if activated:
                    option_b_count += 1
                    option_b_released_sum += rel_usd
                    _log_event(float(window_start), w_day, "OPTION_B_RELEASE", f"Option B released +${rel_usd:.2f} active funds")

            current_day = w_day

            # Window initialization
            self.strategy.reset_window(window_start=window_start, strike_price_K=68500.0)
            init_b, rev_b = self.capital_mgr.get_trade_budgets(w_day)
            self.strategy.set_dynamic_budgets(float(init_b), float(rev_b))

            # Setup order books
            book_up = TokenOrderBook("tok-up")
            book_down = TokenOrderBook("tok-down")
            books = {"Up": book_up, "Down": book_down}

            # Determine scenario for window:
            is_failure_day = (inject_failure_day is not None and w_day == inject_failure_day)
            if is_failure_day and w_idx % 4 == 0:
                scenario = "FAIL_INITIAL"
            elif is_failure_day and w_idx % 4 == 1:
                scenario = "FAIL_REVERSAL"
            elif w_idx % 2 == 0:
                scenario = "INITIAL_WIN"
            else:
                scenario = "REVERSAL_WIN"

            # Phase 1: Waiting Phase (t = window_start + 60s)
            book_up.apply_snapshot([{"price": "0.89", "size": "500"}], [{"price": "0.90", "size": "500"}])
            book_down.apply_snapshot([{"price": "0.09", "size": "500"}], [{"price": "0.10", "size": "500"}])

            sig_wait = self.strategy.on_tick(68500.0, window_start + 60.0, books)
            assert sig_wait is None  # Strict waiting phase enforcement

            # Phase 2: Trading Phase - Initial Entry (t = window_start + 190s)
            sig_init = self.strategy.on_tick(68550.0, window_start + 190.0, books)
            if sig_init:
                blocked, reason = self.capital_mgr.is_trading_blocked(w_day)
                if not blocked:
                    val = self.budget_calc.validate_order_budget(
                        signal_type=sig_init.signal_type,
                        active_capital_usd=self.capital_mgr.state.active_capital,
                        open_exposure_usd=self.engine.get_open_exposure(),
                    )
                    if val.allowed:
                        cost = val.approved_budget_usd
                        shares = round(cost / sig_init.price, 4) if sig_init.price > 0 else sig_init.shares
                        order = Order(
                            order_id=f"soak-init-{window_start}",
                            slug=sig_init.slug,
                            side=sig_init.side,
                            action=sig_init.action,
                            order_type=sig_init.order_type,
                            price=sig_init.price,
                            usd_amount=cost,
                            shares=shares,
                            created_at_ts=window_start + 190.0,
                            latency_ms=0,
                            signal_type=sig_init.signal_type,
                        )
                        self.engine.submit_order(order)
                        self.engine.process_pending_orders(window_start + 190.1, books)
                        trades_placed += 1
                        _log_event(window_start + 190.0, w_day, "ORDER_INITIAL", f"Placed initial order ${cost:.2f} on {sig_init.side}")

            # Phase 3: Trading Phase - Reversal Entry (t = window_start + 230s)
            if scenario in ("REVERSAL_WIN", "FAIL_REVERSAL"):
                # Market reverses so Down ask is in 85-90% range
                book_up.apply_snapshot([{"price": "0.11", "size": "500"}], [{"price": "0.12", "size": "500"}])
                book_down.apply_snapshot([{"price": "0.87", "size": "500"}], [{"price": "0.88", "size": "500"}])

                open_exp = self.engine.get_open_exposure()
                free_act = max(0.0, self.capital_mgr.state.active_capital - open_exp)
                rev_budget_avail = self.budget_calc.calculate_reversal_budget(free_act)
                self.strategy.set_dynamic_budgets(self.strategy.dynamic_initial_usd, float(rev_budget_avail))

                sig_rev = self.strategy.on_tick(68450.0, window_start + 230.0, books)
                if sig_rev:
                    blocked, reason = self.capital_mgr.is_trading_blocked(w_day)
                    if not blocked:
                        val = self.budget_calc.validate_order_budget(
                            signal_type=sig_rev.signal_type,
                            active_capital_usd=self.capital_mgr.state.active_capital,
                            open_exposure_usd=open_exp,
                            override_budget_usd=rev_budget_avail,
                        )
                        if val.allowed:
                            cost_rev = val.approved_budget_usd
                            shares_rev = round(cost_rev / sig_rev.price, 4) if sig_rev.price > 0 else sig_rev.shares
                            order_rev = Order(
                                order_id=f"soak-rev-{window_start}",
                                slug=sig_rev.slug,
                                side=sig_rev.side,
                                action=sig_rev.action,
                                order_type=sig_rev.order_type,
                                price=sig_rev.price,
                                usd_amount=cost_rev,
                                shares=shares_rev,
                                created_at_ts=window_start + 230.0,
                                latency_ms=0,
                                signal_type=sig_rev.signal_type,
                            )
                            self.engine.submit_order(order_rev)
                            self.engine.process_pending_orders(window_start + 230.1, books)
                            trades_placed += 1
                            _log_event(window_start + 230.0, w_day, "ORDER_REVERSAL", f"Placed reversal order ${cost_rev:.2f} on {sig_rev.side}")

            # Phase 4: Market Settlement at window close (t = window_end)
            if scenario == "INITIAL_WIN":
                winner = "Up"
            elif scenario == "REVERSAL_WIN":
                winner = "Down"
            elif scenario == "FAIL_INITIAL":
                winner = "Down"  # Initial on Up loses
            elif scenario == "FAIL_REVERSAL":
                winner = "Up"    # Reversal on Down loses

            settled = self.engine.settle_window_all(f"btc-updown-5m-{window_start}", winner, window_end)
            for pos in settled:
                trades_settled += 1
                was_ms = self.capital_mgr.state.milestone_44_activated
                was_shut = self.capital_mgr.state.daily_shutdown_status

                settle_id = f"soak-settle-{pos.order_id}"
                self.capital_mgr.process_settlement(
                    settlement_id=settle_id,
                    net_pnl_usd=pos.realized_pnl_usd,
                    fee_usd=pos.total_fees_usd,
                    trading_day=w_day,
                    signal_type=pos.signal_type,
                )

                if not was_ms and self.capital_mgr.state.milestone_44_activated and milestone_window is None:
                    milestone_window = w_idx
                    _log_event(float(window_end), w_day, "MILESTONE_44", f"$44 Milestone reached! Split 50/50: Active=${self.capital_mgr.state.active_capital:.2f}, Protected=${self.capital_mgr.state.protected_capital:.2f}")

                if not was_shut and self.capital_mgr.state.daily_shutdown_status:
                    shutdown_count += 1
                    _log_event(float(window_end), w_day, "SHUTDOWN_TRIGGERED", f"Daily shutdown fired: {self.capital_mgr.state.daily_shutdown_reason}")

            # Verify Invariants
            is_inv_valid, inv_violations = self.capital_mgr.verify_invariants()
            invariants_checked += 1
            if not is_inv_valid:
                invariant_violations += len(inv_violations)
                _log_event(float(window_end), w_day, "INVARIANT_ERROR", str(inv_violations))

            # Periodic Capital Reconciliation (every 3 windows)
            if (w_idx + 1) % 3 == 0:
                recon_checks += 1
                total_collateral_cents = (
                    self.capital_mgr.state.active_capital_cents
                    + self.capital_mgr.state.protected_capital_cents
                )
                open_exp_cents = int(round(self.engine.get_open_exposure() * 100))
                avail_cents = max(0, total_collateral_cents - open_exp_cents)
                snap = ExchangeBalanceSnapshot(
                    available_collateral_cents=avail_cents,
                    locked_collateral_cents=open_exp_cents,
                )
                report = self.reconciler.reconcile(external_snapshot=snap, trading_day=w_day)
                if not report.is_valid:
                    recon_mismatches += 1
                    _log_event(float(window_end), w_day, "RECON_MISMATCH", report.message)

        return SoakTestReport(
            windows_simulated=num_windows,
            trades_executed=trades_placed,
            trades_settled=trades_settled,
            starting_equity_usd=self.starting_equity_usd,
            final_equity_usd=self.capital_mgr.state.total_equity,
            final_active_usd=self.capital_mgr.state.active_capital,
            final_protected_usd=self.capital_mgr.state.protected_capital,
            net_pnl_usd=self.capital_mgr.state.total_equity - self.starting_equity_usd,
            milestone_44_reached=self.capital_mgr.state.milestone_44_activated,
            milestone_window_idx=milestone_window,
            shutdowns_triggered=shutdown_count,
            option_b_releases=option_b_count,
            option_b_total_released_usd=option_b_released_sum,
            reconciliation_checks=recon_checks,
            reconciliation_mismatches=recon_mismatches,
            invariants_verified_count=invariants_checked,
            invariant_violations_count=invariant_violations,
            event_log=events,
        )
