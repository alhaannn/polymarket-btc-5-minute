"""
Historical CSV Replay & Capital Allocation Simulation Engine (Roadmap Phase 8).
Compares:
1. Fixed-size baseline behaviour ($1 initial, $10 reversal).
2. Dynamic-sizing compounding without capital protection.
3. Dynamic-sizing compounding with $44 milestone protection (no reserve release).
4. Full system: Dynamic-sizing + $44 milestone protection + Option B next-day reserve release.

DISCLAIMER: All historical replays are simulations intended for strategy validation.
Historical performance does not guarantee future trading profitability.
"""
from __future__ import annotations

import csv
import datetime
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polybot.capital.manager import CapitalManager
from polybot.capital.sizing import round_half_up_to_whole_dollar

log = logging.getLogger("polybot.capital.simulation")


class ReplayMode(str, Enum):
    FIXED_SIZING = "FIXED_SIZING"
    DYNAMIC_NO_PROTECTION = "DYNAMIC_NO_PROTECTION"
    DYNAMIC_WITH_PROTECTION = "DYNAMIC_WITH_PROTECTION"
    OPTION_B_RESERVE_RELEASE = "OPTION_B_RESERVE_RELEASE"


@dataclass
class ReplayTradeResult:
    trade_idx: int
    order_id: str
    slug: str
    signal_type: str
    trading_day: str
    stake_usd: float
    raw_pnl_usd: float
    scaled_pnl_usd: float
    active_before_usd: float
    active_after_usd: float
    protected_before_usd: float
    protected_after_usd: float
    total_equity_usd: float
    initial_budget_usd: int
    reversal_budget_usd: int
    milestone_activated: bool = False
    shutdown_triggered: bool = False
    reserve_released: bool = False


@dataclass
class SimulationSummary:
    mode: ReplayMode
    starting_equity_usd: float
    final_equity_usd: float
    final_active_usd: float
    final_protected_usd: float
    total_net_pnl_usd: float
    total_trades: int
    initial_trades: int
    reversal_trades: int
    win_rate_pct: float
    milestone_44_reached: bool
    milestone_trade_idx: Optional[int]
    shutdowns_triggered_count: int
    option_b_releases_count: int
    max_equity_usd: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    trades: List[ReplayTradeResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "starting_equity_usd": self.starting_equity_usd,
            "final_equity_usd": round(self.final_equity_usd, 2),
            "final_active_usd": round(self.final_active_usd, 2),
            "final_protected_usd": round(self.final_protected_usd, 2),
            "total_net_pnl_usd": round(self.total_net_pnl_usd, 2),
            "total_trades": self.total_trades,
            "initial_trades": self.initial_trades,
            "reversal_trades": self.reversal_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "milestone_44_reached": self.milestone_44_reached,
            "milestone_trade_idx": self.milestone_trade_idx,
            "shutdowns_triggered_count": self.shutdowns_triggered_count,
            "option_b_releases_count": self.option_b_releases_count,
            "max_equity_usd": round(self.max_equity_usd, 2),
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
        }


class HistoricalReplayEngine:
    """
    Executes historical replay simulations on real paper trade CSV logs.
    Preserves exact fill prices, timestamps, and settlement outcomes.
    """

    @staticmethod
    def load_trades_csv(csv_path: Path | str) -> List[Dict[str, Any]]:
        """
        Parses CSV records and filters valid historical trades, sorting chronologically by exec_ts.
        """
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Historical trade log not found at {path}")

        valid_trades: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exec_ts = float(row.get("exec_ts") or 0.0)
                # Filter out synthetic unit test entries with low artificial timestamps (< 1000000000)
                if exec_ts < 1000000000.0:
                    continue

                status = row.get("status", "").upper()
                if status != "SETTLED":
                    continue

                valid_trades.append({
                    "order_id": row.get("order_id", ""),
                    "slug": row.get("slug", ""),
                    "signal_type": row.get("signal_type", "INITIAL").upper(),
                    "side": row.get("side", ""),
                    "stake_usd": float(row.get("stake_usd") or 1.0),
                    "fee_usd": float(row.get("fee_usd") or 0.0),
                    "pnl_usd": float(row.get("pnl_usd") or 0.0),
                    "winner": row.get("winner", ""),
                    "exec_ts": exec_ts,
                    "settled_at_ts": float(row.get("settled_at_ts") or exec_ts),
                })

        # Sort chronologically (oldest trade first)
        valid_trades.sort(key=lambda t: t["exec_ts"])
        return valid_trades

    @staticmethod
    def run_simulation(
        trades: List[Dict[str, Any]],
        mode: ReplayMode,
        starting_equity_usd: float = 11.0,
        temp_db_path: Optional[Path] = None,
        inject_failure_day: Optional[str] = None,
    ) -> SimulationSummary:
        """
        Executes historical trade replay under the specified capital management mode.
        """
        import tempfile
        db_file = temp_db_path or Path(tempfile.mktemp(suffix=".db"))

        capital_mgr = CapitalManager(db_path=db_file, starting_equity_usd=starting_equity_usd)

        # In Mode 2 (no protection), disable milestone trigger by placing milestone out of reach
        if mode == ReplayMode.DYNAMIC_NO_PROTECTION:
            capital_mgr.state.milestone_44_activated = False

        results: List[ReplayTradeResult] = []
        milestone_idx: Optional[int] = None
        shutdown_count = 0
        reserve_release_count = 0
        peak_equity = starting_equity_usd
        max_drawdown_usd = 0.0
        wins = 0

        current_day = ""

        for idx, trade in enumerate(trades):
            exec_ts = trade["exec_ts"]
            trade_day = datetime.datetime.fromtimestamp(exec_ts, datetime.timezone.utc).strftime("%Y-%m-%d")

            # Check day rollover & next-day reserve activation
            if current_day and trade_day != current_day:
                capital_mgr.check_day_rollover(trade_day)
                if mode == ReplayMode.OPTION_B_RESERVE_RELEASE:
                    activated, rel_usd, _ = capital_mgr.check_and_activate_next_day_reserve(trade_day)
                    if activated:
                        reserve_release_count += 1

            current_day = trade_day

            # Determine trade sizing based on mode
            sig_type = trade["signal_type"]
            act_before = capital_mgr.state.active_capital
            prot_before = capital_mgr.state.protected_capital
            tot_before = capital_mgr.state.total_equity

            init_budget, rev_budget = capital_mgr.get_trade_budgets(trade_day)

            if mode == ReplayMode.FIXED_SIZING:
                # Baseline: fixed $1.00 initial, $10.00 reversal
                budget = 1.0 if sig_type == "INITIAL" else 10.0
                scale_factor = 1.0
            else:
                # Dynamic sizing based on active capital pool
                budget = float(init_budget if sig_type == "INITIAL" else rev_budget)
                baseline = 1.0 if sig_type == "INITIAL" else 10.0
                scale_factor = budget / baseline if baseline > 0 else 1.0

            raw_pnl = trade["pnl_usd"]
            raw_fee = trade["fee_usd"]
            raw_stake = trade["stake_usd"]

            # Scaled P&L based on dynamic budget and actual market price/shares
            if raw_stake > 0:
                return_rate = raw_pnl / raw_stake
                scaled_pnl = round(budget * return_rate, 4)
                scaled_fee = round(budget * (raw_fee / raw_stake), 4)
            else:
                scaled_pnl = raw_pnl
                scaled_fee = raw_fee

            # Inject a synthetic reversal failure if requested to test Option B release
            if inject_failure_day and trade_day == inject_failure_day and sig_type == "REVERSAL":
                scaled_pnl = -budget  # Full reversal failure

            if scaled_pnl >= 0:
                wins += 1

            # Process settlement in capital manager
            settle_id = f"sim-{trade['order_id']}-{idx}"

            if mode == ReplayMode.DYNAMIC_NO_PROTECTION:
                # Simply compound active capital directly without milestone split
                cents_pnl = int(round(scaled_pnl * 100))
                capital_mgr.state.active_capital_cents += cents_pnl
                capital_mgr.state.total_equity_cents = capital_mgr.state.active_capital_cents
            else:
                was_ms = capital_mgr.state.milestone_44_activated
                was_shut = capital_mgr.state.daily_shutdown_status

                capital_mgr.process_settlement(
                    settlement_id=settle_id,
                    net_pnl_usd=scaled_pnl,
                    fee_usd=scaled_fee,
                    trading_day=trade_day,
                    signal_type=sig_type,
                )

                if not was_ms and capital_mgr.state.milestone_44_activated and milestone_idx is None:
                    milestone_idx = idx

                if not was_shut and capital_mgr.state.daily_shutdown_status:
                    shutdown_count += 1

            act_after = capital_mgr.state.active_capital
            prot_after = capital_mgr.state.protected_capital
            tot_after = capital_mgr.state.total_equity

            # Track peak equity and drawdown
            if tot_after > peak_equity:
                peak_equity = tot_after
            dd = peak_equity - tot_after
            if dd > max_drawdown_usd:
                max_drawdown_usd = dd

            results.append(ReplayTradeResult(
                trade_idx=idx,
                order_id=trade["order_id"],
                slug=trade["slug"],
                signal_type=sig_type,
                trading_day=trade_day,
                stake_usd=budget,
                raw_pnl_usd=raw_pnl,
                scaled_pnl_usd=scaled_pnl,
                active_before_usd=act_before,
                active_after_usd=act_after,
                protected_before_usd=prot_before,
                protected_after_usd=prot_after,
                total_equity_usd=tot_after,
                initial_budget_usd=init_budget,
                reversal_budget_usd=rev_budget,
                milestone_activated=(milestone_idx == idx),
                shutdown_triggered=capital_mgr.state.daily_shutdown_status,
                reserve_released=(reserve_release_count > 0),
            ))

        final_equity = capital_mgr.state.total_equity
        final_active = capital_mgr.state.active_capital
        final_protected = capital_mgr.state.protected_capital
        total_pnl = final_equity - starting_equity_usd
        win_rate = (wins / len(trades) * 100.0) if trades else 0.0
        max_dd_pct = (max_drawdown_usd / peak_equity * 100.0) if peak_equity > 0 else 0.0

        # Cleanup temporary SQLite db
        try:
            if db_file.exists():
                db_file.unlink()
        except Exception:
            pass

        return SimulationSummary(
            mode=mode,
            starting_equity_usd=starting_equity_usd,
            final_equity_usd=final_equity,
            final_active_usd=final_active,
            final_protected_usd=final_protected,
            total_net_pnl_usd=total_pnl,
            total_trades=len(trades),
            initial_trades=sum(1 for t in trades if t["signal_type"] == "INITIAL"),
            reversal_trades=sum(1 for t in trades if t["signal_type"] == "REVERSAL"),
            win_rate_pct=win_rate,
            milestone_44_reached=capital_mgr.state.milestone_44_activated,
            milestone_trade_idx=milestone_idx,
            shutdowns_triggered_count=shutdown_count,
            option_b_releases_count=reserve_release_count,
            max_equity_usd=peak_equity,
            max_drawdown_usd=max_drawdown_usd,
            max_drawdown_pct=max_dd_pct,
            trades=results,
        )

    @classmethod
    def compare_all_modes(
        cls,
        csv_path: Path | str,
        starting_equity_usd: float = 11.0,
        inject_failure_day: Optional[str] = None,
    ) -> Dict[str, SimulationSummary]:
        """Runs all 4 replay modes across the historical trade dataset."""
        trades = cls.load_trades_csv(csv_path)

        summaries: Dict[str, SimulationSummary] = {}
        for mode in [
            ReplayMode.FIXED_SIZING,
            ReplayMode.DYNAMIC_NO_PROTECTION,
            ReplayMode.DYNAMIC_WITH_PROTECTION,
            ReplayMode.OPTION_B_RESERVE_RELEASE,
        ]:
            summaries[mode.value] = cls.run_simulation(
                trades=trades,
                mode=mode,
                starting_equity_usd=starting_equity_usd,
                inject_failure_day=inject_failure_day,
            )
        return summaries
