"""
Database & State Reset Utility.
Safely wipes historical trades and resets capital ledger to canonical $11.00 starting equity.
Creates a backup before modification.
Can be executed as: python -m polybot.reset
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from polybot.tz import ist_today_str, ist_time_str

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polybot.reset")


def reset_paper_database(
    db_path: Path | str = Path("data/paper_ledger.db"),
    starting_equity_usd: float = 11.0,
    backup: bool = True,
) -> bool:
    db_file = Path(db_path)
    if not db_file.exists():
        log.warning("Database %s does not exist yet. It will be initialized on first run.", db_file)
        return True

    now = time.time()
    today_ist = ist_today_str(now)
    time_ist = ist_time_str(now)

    # 1. Create timestamped backup
    if backup:
        bak_file = db_file.with_name(f"{db_file.name}.bak.{int(now)}")
        try:
            shutil.copy2(db_file, bak_file)
            log.info("Backed up database to %s", bak_file)
        except Exception as e:
            log.warning("Failed to create database backup: %s", e)

    # 2. Reset tables
    cents = int(round(starting_equity_usd * 100))
    conn = sqlite3.connect(str(db_file), timeout=15.0)
    try:
        cur = conn.cursor()

        # Check existing trades count
        trade_count = 0
        try:
            row = cur.execute("SELECT count(*) FROM paper_trades").fetchone()
            trade_count = row[0] if row else 0
        except sqlite3.OperationalError:
            pass

        # Clear trades
        try:
            cur.execute("DELETE FROM paper_trades")
            log.info("Deleted %d records from paper_trades", trade_count)
        except sqlite3.OperationalError:
            pass

        # Clear capital history
        try:
            cur.execute("DELETE FROM capital_transactions")
            cur.execute("DELETE FROM processed_settlements")
            cur.execute("DELETE FROM shutdown_events")
            cur.execute("DELETE FROM capital_reconciliations")
            log.info("Cleared capital transactions, settlements, shutdown events, and reconciliations")
        except sqlite3.OperationalError:
            pass

        # Re-initialize singleton capital state
        cur.execute("""
            CREATE TABLE IF NOT EXISTS capital_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_equity_cents INTEGER NOT NULL,
                active_capital_cents INTEGER NOT NULL,
                protected_capital_cents INTEGER NOT NULL,
                realized_profit_cents INTEGER NOT NULL,
                realized_loss_cents INTEGER NOT NULL,
                active_shortfall_cents INTEGER NOT NULL DEFAULT 0,
                milestone_44_activated INTEGER NOT NULL DEFAULT 0,
                daily_shutdown_status INTEGER NOT NULL DEFAULT 0,
                daily_shutdown_reason TEXT NOT NULL DEFAULT '',
                daily_shutdown_trading_day TEXT NOT NULL DEFAULT '',
                daily_failed_initial_count INTEGER NOT NULL DEFAULT 0,
                daily_failed_reversal_count INTEGER NOT NULL DEFAULT 0,
                daily_failure_trading_day TEXT NOT NULL DEFAULT '',
                next_day_activation_eligible INTEGER NOT NULL DEFAULT 0,
                last_activated_shutdown_event_id TEXT,
                last_activation_trading_day TEXT,
                last_processed_settlement_id TEXT,
                last_reconciliation_ts REAL NOT NULL DEFAULT 0.0,
                last_reconciliation_status TEXT NOT NULL DEFAULT 'OK',
                last_reconciliation_diff_cents INTEGER NOT NULL DEFAULT 0,
                reconciliation_blocked INTEGER NOT NULL DEFAULT 0,
                capital_ledger_version INTEGER NOT NULL DEFAULT 1,
                updated_at_ts REAL NOT NULL
            )
        """)

        cur.execute("""
            INSERT OR REPLACE INTO capital_state (
                id, total_equity_cents, active_capital_cents, protected_capital_cents,
                realized_profit_cents, realized_loss_cents, active_shortfall_cents,
                milestone_44_activated, daily_shutdown_status, daily_shutdown_reason,
                daily_shutdown_trading_day, daily_failed_initial_count,
                daily_failed_reversal_count, daily_failure_trading_day,
                next_day_activation_eligible, last_activated_shutdown_event_id,
                last_activation_trading_day, last_processed_settlement_id,
                last_reconciliation_ts, last_reconciliation_status,
                last_reconciliation_diff_cents, reconciliation_blocked,
                capital_ledger_version, updated_at_ts
            ) VALUES (
                1, ?, ?, 0, 0, 0, 0, 0, 0, '', '', 0, 0, ?, 0, NULL, NULL, NULL, 0.0, 'OK', 0, 0, 1, ?
            )
        """, (cents, cents, today_ist, now))

        # Re-insert canonical initial deposit transaction
        tx_id = f"tx-init-{int(now * 1000)}"
        cur.execute("""
            CREATE TABLE IF NOT EXISTS capital_transactions (
                tx_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                active_before_cents INTEGER NOT NULL,
                active_after_cents INTEGER NOT NULL,
                protected_before_cents INTEGER NOT NULL,
                protected_after_cents INTEGER NOT NULL,
                total_before_cents INTEGER NOT NULL,
                total_after_cents INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL DEFAULT 0,
                settlement_id TEXT,
                trading_day TEXT NOT NULL,
                reason TEXT NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL,
                created_at_ts REAL NOT NULL
            )
        """)

        cur.execute("""
            INSERT INTO capital_transactions (
                tx_id, event_type, amount_cents, active_before_cents, active_after_cents,
                protected_before_cents, protected_after_cents, total_before_cents,
                total_after_cents, fee_cents, settlement_id, trading_day, reason,
                idempotency_key, created_at_ts
            ) VALUES (
                ?, 'INITIAL_CAPITAL', ?, 0, ?, 0, 0, 0, ?, 0, NULL, ?,
                'Fresh start starting capital initialized', ?, ?
            )
        """, (tx_id, cents, cents, cents, today_ist, f"init-fresh-{int(now)}", now))

        conn.commit()
        log.info(
            "SUCCESS: Database reset complete. Active Capital=$%.2f, Protected=$0.00, Total=$%.2f, PnL=$0.00 [Day=%s %s IST]",
            starting_equity_usd, starting_equity_usd, today_ist, time_ist
        )
        return True
    except Exception as e:
        conn.rollback()
        log.error("Failed to reset database: %s", e)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "data/paper_ledger.db"
    equity = float(sys.argv[2]) if len(sys.argv) > 2 else 11.0
    success = reset_paper_database(db_path=db, starting_equity_usd=equity)
    sys.exit(0 if success else 1)
