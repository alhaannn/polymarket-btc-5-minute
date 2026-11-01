"""
ACID SQLite Capital Ledger.
Manages durable state, idempotent transactions, processed settlement records,
and shutdown activation events.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polybot.capital.models import CapitalState, CapitalTransaction, TransactionType

log = logging.getLogger("polybot.capital.ledger")


class CapitalLedger:
    """ACID-compliant SQLite store for capital state and audit history."""

    def __init__(self, db_path: Path | str = Path("data/paper_ledger.db")):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            # 1. Singleton capital state table
            conn.execute("""
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
                    next_day_activation_eligible INTEGER NOT NULL DEFAULT 0,
                    last_activated_shutdown_event_id TEXT,
                    last_activation_trading_day TEXT,
                    last_processed_settlement_id TEXT,
                    capital_ledger_version INTEGER NOT NULL DEFAULT 1,
                    updated_at_ts REAL NOT NULL
                )
            """)

            # Schema migration: ensure active_shortfall_cents column exists
            cols = [col[1] for col in conn.execute("PRAGMA table_info(capital_state)").fetchall()]
            if "active_shortfall_cents" not in cols:
                conn.execute("ALTER TABLE capital_state ADD COLUMN active_shortfall_cents INTEGER NOT NULL DEFAULT 0")

            # 2. Immutable transaction history table
            conn.execute("""
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cap_tx_day ON capital_transactions(trading_day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cap_tx_event ON capital_transactions(event_type)")

            # 3. Processed settlements tracking table for exact-once processing
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_settlements (
                    settlement_id TEXT PRIMARY KEY,
                    net_pnl_cents INTEGER NOT NULL,
                    fee_cents INTEGER NOT NULL DEFAULT 0,
                    processed_at_ts REAL NOT NULL
                )
            """)

            # 4. Shutdown events for Option B reserve activation tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shutdown_events (
                    event_id TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL UNIQUE,
                    failed_initial_count INTEGER NOT NULL,
                    failed_reversal_count INTEGER NOT NULL,
                    activated INTEGER NOT NULL DEFAULT 0,
                    activated_at_ts REAL,
                    created_at_ts REAL NOT NULL
                )
            """)
            conn.commit()

    def get_state(self) -> Optional[CapitalState]:
        """Loads current capital state or None if not initialized."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM capital_state WHERE id = 1").fetchone()
            if not row:
                return None
            row_keys = row.keys()
            return CapitalState(
                total_equity_cents=row["total_equity_cents"],
                active_capital_cents=row["active_capital_cents"],
                protected_capital_cents=row["protected_capital_cents"],
                realized_profit_cents=row["realized_profit_cents"],
                realized_loss_cents=row["realized_loss_cents"],
                active_shortfall_cents=row["active_shortfall_cents"] if "active_shortfall_cents" in row_keys else 0,
                milestone_44_activated=bool(row["milestone_44_activated"]),
                daily_shutdown_status=bool(row["daily_shutdown_status"]),
                daily_shutdown_reason=row["daily_shutdown_reason"],
                daily_shutdown_trading_day=row["daily_shutdown_trading_day"],
                next_day_activation_eligible=bool(row["next_day_activation_eligible"]),
                last_activated_shutdown_event_id=row["last_activated_shutdown_event_id"],
                last_activation_trading_day=row["last_activation_trading_day"],
                last_processed_settlement_id=row["last_processed_settlement_id"],
                capital_ledger_version=row["capital_ledger_version"],
                updated_at_ts=row["updated_at_ts"],
            )

    def save_state_and_transaction(
        self,
        state: CapitalState,
        tx: CapitalTransaction,
        processed_settlement: Optional[Dict[str, Any]] = None,
        shutdown_event: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically persists updated state and the corresponding audit transaction."""
        now = time.time()
        state.updated_at_ts = now
        tx.created_at_ts = now

        with self._get_connection() as conn:
            # 1. Update singleton state
            conn.execute("""
                INSERT OR REPLACE INTO capital_state (
                    id, total_equity_cents, active_capital_cents, protected_capital_cents,
                    realized_profit_cents, realized_loss_cents, active_shortfall_cents,
                    milestone_44_activated, daily_shutdown_status, daily_shutdown_reason,
                    daily_shutdown_trading_day, next_day_activation_eligible,
                    last_activated_shutdown_event_id, last_activation_trading_day,
                    last_processed_settlement_id, capital_ledger_version, updated_at_ts
                ) VALUES (
                    1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                state.total_equity_cents,
                state.active_capital_cents,
                state.protected_capital_cents,
                state.realized_profit_cents,
                state.realized_loss_cents,
                state.active_shortfall_cents,
                1 if state.milestone_44_activated else 0,
                1 if state.daily_shutdown_status else 0,
                state.daily_shutdown_reason,
                state.daily_shutdown_trading_day,
                1 if state.next_day_activation_eligible else 0,
                state.last_activated_shutdown_event_id,
                state.last_activation_trading_day,
                state.last_processed_settlement_id,
                state.capital_ledger_version,
                state.updated_at_ts,
            ))

            # 2. Insert transaction record
            conn.execute("""
                INSERT INTO capital_transactions (
                    tx_id, event_type, amount_cents, active_before_cents, active_after_cents,
                    protected_before_cents, protected_after_cents, total_before_cents,
                    total_after_cents, fee_cents, settlement_id, trading_day, reason,
                    idempotency_key, created_at_ts
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                tx.tx_id,
                tx.event_type.value if isinstance(tx.event_type, TransactionType) else str(tx.event_type),
                tx.amount_cents,
                tx.active_before_cents,
                tx.active_after_cents,
                tx.protected_before_cents,
                tx.protected_after_cents,
                tx.total_before_cents,
                tx.total_after_cents,
                tx.fee_cents,
                tx.settlement_id,
                tx.trading_day,
                tx.reason,
                tx.idempotency_key,
                tx.created_at_ts,
            ))

            # 3. Mark settlement processed if provided
            if processed_settlement:
                conn.execute("""
                    INSERT OR REPLACE INTO processed_settlements (
                        settlement_id, net_pnl_cents, fee_cents, processed_at_ts
                    ) VALUES (?, ?, ?, ?)
                """, (
                    processed_settlement["settlement_id"],
                    processed_settlement["net_pnl_cents"],
                    processed_settlement.get("fee_cents", 0),
                    now,
                ))

            # 4. Insert or update shutdown event if provided
            if shutdown_event:
                conn.execute("""
                    INSERT OR REPLACE INTO shutdown_events (
                        event_id, trading_day, failed_initial_count, failed_reversal_count,
                        activated, activated_at_ts, created_at_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    shutdown_event["event_id"],
                    shutdown_event["trading_day"],
                    shutdown_event.get("failed_initial_count", 0),
                    shutdown_event.get("failed_reversal_count", 0),
                    1 if shutdown_event.get("activated", False) else 0,
                    shutdown_event.get("activated_at_ts"),
                    shutdown_event.get("created_at_ts", now),
                ))

            conn.commit()

    def is_settlement_processed(self, settlement_id: str) -> bool:
        """Checks if a settlement has already been processed (idempotency guard)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_settlements WHERE settlement_id = ?",
                (settlement_id,)
            ).fetchone()
            return row is not None

    def get_unactivated_shutdown_event(self) -> Optional[Dict[str, Any]]:
        """Returns the oldest unactivated qualifying daily shutdown event, if any."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM shutdown_events WHERE activated = 0 ORDER BY created_at_ts ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def mark_shutdown_event_activated(self, event_id: str, activated_at_ts: float) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE shutdown_events SET activated = 1, activated_at_ts = ? WHERE event_id = ?",
                (activated_at_ts, event_id)
            )
            conn.commit()

    def get_recent_transactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves recent capital transaction history for audit logs and UI."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM capital_transactions ORDER BY created_at_ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def reconcile_with_external(self, external_balance_cents: int) -> Tuple[bool, int]:
        """
        Validates internal total equity against an external ledger or exchange balance.
        Returns: (in_sync: bool, difference_cents: int) where difference = external - internal.
        """
        state = self.get_state()
        if not state:
            return False, external_balance_cents
        diff = external_balance_cents - state.total_equity_cents
        return (diff == 0), diff
