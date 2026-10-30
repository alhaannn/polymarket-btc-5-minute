"""
Persistent SQLite Trade Ledger for auditable paper trading records and analytics.
Stores trade events, fills, and settlements with millisecond-precision timestamps.
Calculates hourly, daily, and lifetime P&L, win rates, and activity metrics.
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("polybot.execution.ledger")


class TradeLedger:
    """ACID-compliant SQLite trade repository that survives process restarts."""

    def __init__(self, db_path: Path | str = Path("data/paper_ledger.db")):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    order_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    window_start INTEGER,
                    window_end INTEGER,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    linked_order_id TEXT,
                    stake_usd REAL NOT NULL,
                    shares REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    fee_usd REAL NOT NULL,
                    trigger_prob REAL,
                    counterpart_prob REAL,
                    data_receipt_ts REAL,
                    signal_detection_ts REAL,
                    exec_ts REAL,
                    latency_ms REAL,
                    status TEXT NOT NULL,
                    winner TEXT,
                    payout_usd REAL,
                    pnl_usd REAL,
                    settled_at_ts REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_slug ON paper_trades(slug)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON paper_trades(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_ts ON paper_trades(exec_ts)")
            conn.commit()

    def record_trade(
        self,
        order_id: str,
        slug: str,
        side: str,
        action: str,
        order_type: str,
        signal_type: str,
        stake_usd: float,
        shares: float,
        avg_price: float,
        fee_usd: float,
        exec_ts: float,
        window_start: Optional[int] = None,
        window_end: Optional[int] = None,
        linked_order_id: Optional[str] = None,
        trigger_prob: Optional[float] = None,
        counterpart_prob: Optional[float] = None,
        data_receipt_ts: Optional[float] = None,
        signal_detection_ts: Optional[float] = None,
        latency_ms: float = 0.0,
        status: str = "OPEN",
    ) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO paper_trades (
                    order_id, slug, window_start, window_end, side, action, order_type,
                    signal_type, linked_order_id, stake_usd, shares, avg_price, fee_usd,
                    trigger_prob, counterpart_prob, data_receipt_ts, signal_detection_ts,
                    exec_ts, latency_ms, status, winner, payout_usd, pnl_usd, settled_at_ts
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, NULL
                )
            """, (
                order_id, slug, window_start, window_end, side, action, order_type,
                signal_type, linked_order_id, stake_usd, shares, avg_price, fee_usd,
                trigger_prob, counterpart_prob, data_receipt_ts, signal_detection_ts,
                exec_ts, latency_ms, status
            ))
            conn.commit()

    def settle_order(
        self,
        order_id: str,
        winner: str,
        payout_usd: float,
        pnl_usd: float,
        settled_at_ts: float,
    ) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("""
                UPDATE paper_trades
                SET status = 'SETTLED',
                    winner = ?,
                    payout_usd = ?,
                    pnl_usd = ?,
                    settled_at_ts = ?
                WHERE order_id = ?
            """, (winner, payout_usd, pnl_usd, settled_at_ts, order_id))
            conn.commit()
            return cur.rowcount > 0

    def settle_slug(
        self,
        slug: str,
        winner: str,
        settled_at_ts: float,
    ) -> List[Dict[str, Any]]:
        """Settle all open positions associated with a market slug."""
        settled_trades = []
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE slug = ? AND status = 'OPEN'",
                (slug,)
            ).fetchall()

            for r in rows:
                is_win = (r["side"] == winner)
                payout = round(r["shares"] if is_win else 0.0, 4)
                pnl = round(payout - r["stake_usd"] - r["fee_usd"], 4)

                conn.execute("""
                    UPDATE paper_trades
                    SET status = 'SETTLED',
                        winner = ?,
                        payout_usd = ?,
                        pnl_usd = ?,
                        settled_at_ts = ?
                    WHERE order_id = ?
                """, (winner, payout, pnl, settled_at_ts, r["order_id"]))

                trade_data = dict(r)
                trade_data.update(
                    status="SETTLED",
                    winner=winner,
                    payout_usd=payout,
                    pnl_usd=pnl,
                    settled_at_ts=settled_at_ts,
                )
                settled_trades.append(trade_data)

            conn.commit()
        return settled_trades

    def get_open_trades(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY exec_ts ASC").fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM paper_trades ORDER BY exec_ts DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_total_markets_count(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT slug) FROM paper_trades").fetchone()
            return row[0] if row else 0

    def get_performance_summary(self, timezone_mode: str = "local") -> Dict[str, Any]:
        """
        Computes financial and activity metrics for:
        - Current Hour
        - Current Day
        - Lifetime
        """
        now = datetime.datetime.now()
        # Hour start
        hour_start = now.replace(minute=0, second=0, microsecond=0).timestamp()
        # Day start
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        with self._get_connection() as conn:
            all_rows = conn.execute("SELECT * FROM paper_trades").fetchall()

        all_trades = [dict(r) for r in all_rows]

        def _calc_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
            total_trades = len(trades)
            initial_count = sum(1 for t in trades if t["signal_type"] == "INITIAL")
            reversal_count = sum(1 for t in trades if t["signal_type"] == "REVERSAL")
            settled = [t for t in trades if t["status"] == "SETTLED" and t["pnl_usd"] is not None]

            wins = [t for t in settled if t["pnl_usd"] > 0]
            losses = [t for t in settled if t["pnl_usd"] < 0]

            gross_profit = sum(t["pnl_usd"] for t in wins)
            gross_loss = abs(sum(t["pnl_usd"] for t in losses))
            net_pnl = gross_profit - gross_loss

            win_rate = (len(wins) / len(settled) * 100.0) if settled else 0.0

            latencies = [t["latency_ms"] for t in trades if t.get("latency_ms") and t["latency_ms"] > 0]
            avg_lat = (sum(latencies) / len(latencies)) if latencies else 0.0

            return {
                "total_trades": total_trades,
                "initial_trades": initial_count,
                "reversal_trades": reversal_count,
                "settled_trades": len(settled),
                "open_trades": total_trades - len(settled),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(win_rate, 1),
                "gross_profit_usd": round(gross_profit, 2),
                "gross_loss_usd": round(gross_loss, 2),
                "net_pnl_usd": round(net_pnl, 2),
                "avg_latency_ms": round(avg_lat, 1),
            }

        hour_trades = [t for t in all_trades if (t.get("exec_ts") or 0.0) >= hour_start]
        day_trades = [t for t in all_trades if (t.get("exec_ts") or 0.0) >= day_start]

        markets_count = len(set(t["slug"] for t in all_trades))
        reversal_events = sum(1 for t in all_trades if t["signal_type"] == "REVERSAL")

        return {
            "timezone": timezone_mode,
            "markets_observed": markets_count,
            "reversal_events_detected": reversal_events,
            "hour": _calc_metrics(hour_trades),
            "day": _calc_metrics(day_trades),
            "lifetime": _calc_metrics(all_trades),
        }
