"""
Base types and interface for order execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Order:
    order_id: str
    slug: str
    side: str  # "Up" or "Down"
    action: str  # "BUY" or "SELL"
    order_type: str  # "taker" or "maker"
    price: float
    usd_amount: float
    shares: float
    created_at_ts: float
    latency_ms: int = 400
    status: str = "PENDING"  # "PENDING", "FILLED", "CANCELLED", "REJECTED"


@dataclass
class Fill:
    fill_id: str
    order_id: str
    slug: str
    side: str
    action: str
    price: float
    shares: float
    cost_usd: float
    fee_usd: float
    filled_at_ts: float


@dataclass
class Position:
    slug: str
    side: str
    shares: float
    avg_entry_price: float
    total_cost_usd: float
    total_fees_usd: float
    opened_at_ts: float
    status: str = "OPEN"  # "OPEN", "SETTLED", "CLOSED_EARLY"
    settled_at_ts: Optional[float] = None
    settle_winner: Optional[str] = None
    payout_usd: float = 0.0
    realized_pnl_usd: float = 0.0


class ExecutionEngine(ABC):
    """Abstract interface for trade execution engines (paper and live)."""

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass
