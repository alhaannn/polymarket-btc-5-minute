"""
Position sizing engines for polybot.
Supports Fixed Stake and Fractional Kelly Criterion with hard safety caps.
"""
from __future__ import annotations

import logging
from typing import Optional

from polybot.config import RiskConfig

log = logging.getLogger("polybot.sizing")


class PositionSizer:
    """Computes dollar stake per trade based on configuration and model edge."""

    def __init__(self, risk_config: Optional[RiskConfig] = None, default_stake_usd: float = 5.0):
        self.config = risk_config or RiskConfig()
        self.default_stake_usd = default_stake_usd

    def calculate_stake(
        self,
        edge: float,
        model_prob: float,
        price: float,
        bankroll_usd: float = 100.0,
    ) -> float:
        """
        Calculates stake size in dollars:
        - "fixed": returns fixed stake (default $5.0)
        - "fractional_kelly":
            f* = (p - price) / (1 - price) = edge / (1 - price)
            stake = bankroll * f* * kelly_fraction
            hard capped at kelly_max_stake_usd
        """
        if self.config.sizing_mode == "fractional_kelly":
            if edge <= 0 or price >= 1.0 or price <= 0:
                return 0.0

            # Binary Kelly formula: f* = edge / (1 - price)
            full_kelly = edge / (1.0 - price)
            if full_kelly <= 0:
                return 0.0

            # Scale by fraction (e.g. 0.25 for quarter-Kelly)
            fractional_f = full_kelly * self.config.kelly_fraction
            calculated_stake = bankroll_usd * fractional_f

            # Clamp between minimum $1.0 and maximum cap
            capped_stake = max(1.0, min(calculated_stake, self.config.kelly_max_stake_usd))
            log.debug(
                "Kelly sizing: edge=%.3f, p=%.3f, price=%.3f, f*=%.3f -> stake=$%.2f (cap=$%.2f)",
                edge, model_prob, price, full_kelly, capped_stake, self.config.kelly_max_stake_usd
            )
            return round(capped_stake, 2)

        # Default: Fixed stake
        return self.default_stake_usd
