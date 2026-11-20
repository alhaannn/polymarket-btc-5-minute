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


def calculate_polymarket_5m_crypto_fee(shares: float, price: float) -> float:
    """
    Computes exact Polymarket 5-minute crypto taker fee:
    Fee (USDC) = shares * 0.07 * price * (1.0 - price)
    """
    if shares <= 0 or price <= 0 or price >= 1.0:
        return 0.0
    return round(shares * 0.07 * price * (1.0 - price), 6)


def calculate_fee(
    price: float,
    shares: float,
    order_type: str = "taker",
    schedule: FeeSchedule = FeeSchedule(),
    is_5m_crypto: bool = False,
) -> float:
    """
    Computes fee for a trade.
    If is_5m_crypto is True and order_type is taker, uses exact Polymarket crypto taker fee formula.
    Otherwise notional * rate.
    """
    if price <= 0 or shares <= 0:
        return 0.0

    if is_5m_crypto and order_type.lower() == "taker":
        return calculate_polymarket_5m_crypto_fee(shares, price)

    notional = price * shares
    rate = schedule.taker_rate if order_type.lower() == "taker" else schedule.maker_rate
    fee = notional * rate
    return max(schedule.min_fee_usd, fee)
