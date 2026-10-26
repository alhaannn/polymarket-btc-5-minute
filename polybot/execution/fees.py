"""
Polymarket fee schedule and calculation functions.
Ensures realistic fee accounting for both maker and taker orders.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeeSchedule:
    taker_rate: float = 0.001  # 0.10% (10 bps) of notional by default
    maker_rate: float = 0.000  # 0.00% maker rebate/fee
    min_fee_usd: float = 0.000


def calculate_fee(
    price: float,
    shares: float,
    order_type: str = "taker",
    schedule: FeeSchedule = FeeSchedule(),
) -> float:
    """
    Computes fee for a trade.
    Notional = price * shares.
    Fee = notional * rate.
    """
    if price <= 0 or shares <= 0:
        return 0.0

    notional = price * shares
    rate = schedule.taker_rate if order_type.lower() == "taker" else schedule.maker_rate
    fee = notional * rate
    return max(schedule.min_fee_usd, fee)
