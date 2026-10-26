"""
Quantitative backtest reporting engine.
Generates comprehensive performance metrics, breakdown tables,
and statistical significance verdicts against break-even win rates.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from polybot.backtest.engine import BacktestTrade


@dataclass
class PerformanceSummary:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    break_even_win_rate: float
    total_cost_usd: float
    total_fees_usd: float
    total_payout_usd: float
    net_pnl_usd: float
    profit_factor: float
    max_drawdown_usd: float
    longest_losing_streak: int
    brier_score: float
    win_rate_ci_lower: float
    win_rate_ci_upper: float
    statistical_verdict: str


class BacktestReporter:
    """Generates detailed reports and markdown tables from backtest trades."""

    @staticmethod
    def compute_summary(trades: List[BacktestTrade]) -> PerformanceSummary:
        if not trades:
            return PerformanceSummary(
                0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.25, 0.0, 0.0, "No trades recorded"
            )

        n = len(trades)
        wins = sum(1 for t in trades if t.is_win)
        losses = n - wins
        win_rate = (wins / n) * 100.0

        total_cost = sum(t.cost_usd for t in trades)
        total_fees = sum(t.fee_usd for t in trades)
        total_payout = sum(t.payout_usd for t in trades)
        net_pnl = sum(t.pnl_usd for t in trades)

        # Break-even win rate: avg_cost / payout_per_win ($1.0)
        avg_entry_price = sum(t.entry_price for t in trades) / n
        avg_fee_pct = (total_fees / total_cost) if total_cost > 0 else 0.0
        be_win_rate = (avg_entry_price * (1.0 + avg_fee_pct)) * 100.0

        # Profit factor
        gross_profits = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
        gross_losses = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
        profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (99.0 if gross_profits > 0 else 0.0)

        # Max drawdown & longest losing streak
        cum_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        streak = 0
        max_streak = 0

        for t in trades:
            cum_pnl += t.pnl_usd
            if cum_pnl > peak:
                peak = cum_pnl
            dd = peak - cum_pnl
            if dd > max_dd:
                max_dd = dd

            if not t.is_win:
                streak += 1
                if streak > max_streak:
                    max_streak = streak
            else:
                streak = 0

        # Brier Score
        brier_score = sum((t.model_prob - (1.0 if t.is_win else 0.0)) ** 2 for t in trades) / n

        # Wilson score 95% confidence interval for win rate
        ci_lower, ci_upper = BacktestReporter._wilson_score_interval(wins, n, confidence=0.95)

        # Statistical significance verdict
        if n < 30:
            verdict = f"WARNING: Sample size too small (N={n} < 30). Statistical tests are unreliable."
        elif ci_lower * 100.0 > be_win_rate:
            diff = (win_rate - be_win_rate)
            verdict = f"STATISTICALLY SIGNIFICANT ALPHA (+{diff:.2f}% edge above break-even). 95% CI [{ci_lower * 100.0:.1f}%, {ci_upper * 100.0:.1f}%] is strictly above required {be_win_rate:.1f}%."
        elif ci_upper * 100.0 < be_win_rate:
            diff = (be_win_rate - win_rate)
            verdict = f"STATISTICALLY SIGNIFICANT LOSS (-{diff:.2f}% below break-even). Strategy does not overcome entry costs and fees."
        else:
            verdict = f"INCONCLUSIVE: Win rate {win_rate:.1f}% is indistinguishable from break-even {be_win_rate:.1f}% within 95% CI [{ci_lower * 100.0:.1f}%, {ci_upper * 100.0:.1f}%]. Collect more data."

        return PerformanceSummary(
            total_trades=n,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            break_even_win_rate=be_win_rate,
            total_cost_usd=total_cost,
            total_fees_usd=total_fees,
            total_payout_usd=total_payout,
            net_pnl_usd=net_pnl,
            profit_factor=profit_factor,
            max_drawdown_usd=max_dd,
            longest_losing_streak=max_streak,
            brier_score=brier_score,
            win_rate_ci_lower=ci_lower * 100.0,
            win_rate_ci_upper=ci_upper * 100.0,
            statistical_verdict=verdict,
        )

    @staticmethod
    def _wilson_score_interval(k: int, n: int, confidence: float = 0.95) -> Tuple[float, float]:
        """Calculates Wilson score confidence interval for binomial proportions."""
        if n == 0:
            return 0.0, 0.0
        z = 1.95996  # 95% confidence z-value
        p_hat = k / n
        denom = 1.0 + (z ** 2) / n
        center = (p_hat + (z ** 2) / (2 * n)) / denom
        margin = (z * math.sqrt((p_hat * (1 - p_hat) / n) + (z ** 2) / (4 * (n ** 2)))) / denom
        return max(0.0, center - margin), min(1.0, center + margin)

    @staticmethod
    def format_breakdown_table(
        trades: List[BacktestTrade],
        category_name: str,
        group_fn: Any,
    ) -> str:
        """Groups trades and formats into a clean ASCII/Markdown table."""
        groups: Dict[str, List[BacktestTrade]] = defaultdict(list)
        for t in trades:
            k = group_fn(t)
            groups[k].append(t)

        lines = [
            f"| {category_name:<16} | Trades | Win Rate | Net PnL | Avg Ask | Brier Score |",
            f"|{'-' * 18}|{'-' * 8}|{'-' * 10}|{'-' * 9}|{'-' * 9}|{'-' * 13}|",
        ]

        for k in sorted(groups.keys()):
            g_trades = groups[k]
            gn = len(g_trades)
            gwins = sum(1 for t in g_trades if t.is_win)
            gwr = (gwins / gn * 100.0) if gn else 0.0
            gpnl = sum(t.pnl_usd for t in g_trades)
            gask = sum(t.entry_price for t in g_trades) / gn
            gbrier = sum((t.model_prob - (1.0 if t.is_win else 0.0)) ** 2 for t in g_trades) / gn

            lines.append(
                f"| {k:<16} | {gn:>6} | {gwr:>7.1f}% | ${gpnl:>+6.2f} | {gask:>7.3f} | {gbrier:>11.4f} |"
            )

        return "\n".join(lines)

    @classmethod
    def generate_full_report(cls, trades: List[BacktestTrade], strategy_name: str = "edge") -> str:
        """Produces the complete human-readable backtest audit report."""
        summary = cls.compute_summary(trades)

        # Breakdown groupers
        ask_bucket = lambda t: (
            "[0.85, 0.90)" if t.entry_price < 0.90
            else "[0.90, 0.93)" if t.entry_price < 0.93
            else "[0.93, 0.96)" if t.entry_price < 0.96
            else "[0.96, 0.985]"
        )

        time_left_bucket = lambda t: (
            "> 180s left" if (300.0 - t.elapsed_sec) > 180
            else "120s - 180s left" if (300.0 - t.elapsed_sec) > 120
            else "60s - 120s left" if (300.0 - t.elapsed_sec) > 60
            else "< 60s left"
        )

        hour_bucket = lambda t: f"{t.hour_of_day:02d}:00 UTC"

        report = f"""
================================================================================
                    POLYBOT HISTORICAL BACKTEST REPORT
================================================================================
Strategy:              {strategy_name.upper()}
Total Replayed Trades: {summary.total_trades}
Win Count:             {summary.wins}
Loss Count:            {summary.losses}
Win Rate:              {summary.win_rate:.2f}%
95% Confidence Bounds: [{summary.win_rate_ci_lower:.2f}%, {summary.win_rate_ci_upper:.2f}%]
Break-Even Win Rate:   {summary.break_even_win_rate:.2f}%
Net Realized PnL:      ${summary.net_pnl_usd:+.2f}
Total Fees Paid:       ${summary.total_fees_usd:.2f}
Profit Factor:         {summary.profit_factor:.2f}
Max Drawdown:          ${summary.max_drawdown_usd:.2f}
Longest Losing Streak: {summary.longest_losing_streak} windows
Brier Score (MSE):     {summary.brier_score:.4f} (0.0=perfect, 0.25=coin flip)

--------------------------------------------------------------------------------
STATISTICAL SIGNIFICANCE VERDICT
--------------------------------------------------------------------------------
{summary.statistical_verdict}

--------------------------------------------------------------------------------
BREAKDOWN: BY ASK BUCKET
--------------------------------------------------------------------------------
{cls.format_breakdown_table(trades, "Ask Bucket", ask_bucket)}

--------------------------------------------------------------------------------
BREAKDOWN: BY TIME LEFT IN WINDOW
--------------------------------------------------------------------------------
{cls.format_breakdown_table(trades, "Time Left", time_left_bucket)}

--------------------------------------------------------------------------------
BREAKDOWN: BY VOLATILITY REGIME
--------------------------------------------------------------------------------
{cls.format_breakdown_table(trades, "Vol Regime", lambda t: t.vol_regime)}

--------------------------------------------------------------------------------
BREAKDOWN: BY HOUR OF DAY
--------------------------------------------------------------------------------
{cls.format_breakdown_table(trades, "Hour of Day", hour_bucket)}
================================================================================
"""
        return report
