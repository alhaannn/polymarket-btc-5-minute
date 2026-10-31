"""
Data models and transaction types for dynamic capital management.
Uses integer cents internally for zero floating-point drift and exact penny accounting.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class TransactionType(str, Enum):
    INITIAL_CAPITAL = "INITIAL_CAPITAL"
    PRE_MILESTONE_PNL = "PRE_MILESTONE_PNL"
    MILESTONE_44_ACTIVATION = "MILESTONE_44_ACTIVATION"
    PROFIT_ALLOCATION = "PROFIT_ALLOCATION"
    LOSS_DEDUCTION = "LOSS_DEDUCTION"
    RESERVE_RELEASE_OPTION_B = "RESERVE_RELEASE_OPTION_B"
    RECONCILIATION_ADJUSTMENT = "RECONCILIATION_ADJUSTMENT"


@dataclass
class CapitalState:
    """
    Core state snapshot of attributable capital pools and milestone status.
    All balances are tracked in integer cents (e.g. $11.00 = 1100 cents).
    """
    total_equity_cents: int = 1100
    active_capital_cents: int = 1100
    protected_capital_cents: int = 0
    realized_profit_cents: int = 0
    realized_loss_cents: int = 0
    milestone_44_activated: bool = False
    daily_shutdown_status: bool = False
    daily_shutdown_reason: str = ""
    daily_shutdown_trading_day: str = ""
    next_day_activation_eligible: bool = False
    last_activated_shutdown_event_id: Optional[str] = None
    last_activation_trading_day: Optional[str] = None
    last_processed_settlement_id: Optional[str] = None
    capital_ledger_version: int = 1
    updated_at_ts: float = 0.0

    @property
    def total_equity(self) -> float:
        return round(self.total_equity_cents / 100.0, 2)

    @property
    def active_capital(self) -> float:
        return round(self.active_capital_cents / 100.0, 2)

    @property
    def protected_capital(self) -> float:
        return round(self.protected_capital_cents / 100.0, 2)

    @property
    def realized_profit(self) -> float:
        return round(self.realized_profit_cents / 100.0, 2)

    @property
    def realized_loss(self) -> float:
        return round(self.realized_loss_cents / 100.0, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_equity": self.total_equity,
            "active_capital": self.active_capital,
            "protected_capital": self.protected_capital,
            "realized_profit": self.realized_profit,
            "realized_loss": self.realized_loss,
            "total_equity_cents": self.total_equity_cents,
            "active_capital_cents": self.active_capital_cents,
            "protected_capital_cents": self.protected_capital_cents,
            "milestone_44_activated": self.milestone_44_activated,
            "daily_shutdown_status": self.daily_shutdown_status,
            "daily_shutdown_reason": self.daily_shutdown_reason,
            "daily_shutdown_trading_day": self.daily_shutdown_trading_day,
            "next_day_activation_eligible": self.next_day_activation_eligible,
            "last_activated_shutdown_event_id": self.last_activated_shutdown_event_id,
            "last_activation_trading_day": self.last_activation_trading_day,
            "last_processed_settlement_id": self.last_processed_settlement_id,
            "capital_ledger_version": self.capital_ledger_version,
            "updated_at_ts": self.updated_at_ts,
        }


@dataclass
class CapitalTransaction:
    """Immutable audit record for every capital transfer or adjustment."""
    tx_id: str
    event_type: TransactionType
    amount_cents: int
    active_before_cents: int
    active_after_cents: int
    protected_before_cents: int
    protected_after_cents: int
    total_before_cents: int
    total_after_cents: int
    fee_cents: int = 0
    settlement_id: Optional[str] = None
    trading_day: str = ""
    reason: str = ""
    idempotency_key: str = ""
    created_at_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "event_type": self.event_type.value if isinstance(self.event_type, TransactionType) else str(self.event_type),
            "amount_usd": round(self.amount_cents / 100.0, 2),
            "amount_cents": self.amount_cents,
            "active_before": round(self.active_before_cents / 100.0, 2),
            "active_after": round(self.active_after_cents / 100.0, 2),
            "protected_before": round(self.protected_before_cents / 100.0, 2),
            "protected_after": round(self.protected_after_cents / 100.0, 2),
            "total_before": round(self.total_before_cents / 100.0, 2),
            "total_after": round(self.total_after_cents / 100.0, 2),
            "fee_usd": round(self.fee_cents / 100.0, 2),
            "settlement_id": self.settlement_id,
            "trading_day": self.trading_day,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "created_at_ts": self.created_at_ts,
        }
