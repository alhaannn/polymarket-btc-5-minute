"""
Mathematical probability model for Polymarket BTC 5m binary markets.
Implements Brownian motion approximation, dual-horizon EWMA volatility,
fat-tail corrections, and Platt scaling calibration.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


def standard_normal_cdf(x: float) -> float:
    """Computes standard normal cumulative distribution function Phi(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class ModelOutput:
    lead_usd: float
    time_left_sec: float
    vol_per_sec: float
    z_score: float
    p_raw: float
    p_calibrated: float
    brier_score_sample: Optional[float] = None


class RollingEWMAVol:
    """
    Computes Exponentially Weighted Moving Average (EWMA) volatility
    of 1-second price log returns over short and long horizons.
    """

    def __init__(self, short_span: int = 30, long_span: int = 180):
        self.short_alpha = 2.0 / (short_span + 1.0)
        self.long_alpha = 2.0 / (long_span + 1.0)

        self.last_price: Optional[float] = None
        self.last_ts_sec: Optional[float] = None

        self.short_var: float = 0.0
        self.long_var: float = 0.0
        self.initialized: bool = False

    def update(self, price: float, ts_sec: float) -> None:
        if price <= 0:
            return

        if self.last_price is None or self.last_ts_sec is None:
            self.last_price = price
            self.last_ts_sec = ts_sec
            return

        dt = ts_sec - self.last_ts_sec
        if dt < 0.2:  # Ignore sub-second clustering to prevent false variance inflation
            return

        ret = math.log(price / self.last_price)
        # Normalize return to 1-second rate
        ret_1s = ret / math.sqrt(dt)
        var_sample = ret_1s * ret_1s

        if not self.initialized:
            self.short_var = var_sample
            self.long_var = var_sample
            self.initialized = True
        else:
            self.short_var = (1.0 - self.short_alpha) * self.short_var + self.short_alpha * var_sample
            self.long_var = (1.0 - self.long_alpha) * self.long_var + self.long_alpha * var_sample

        self.last_price = price
        self.last_ts_sec = ts_sec

    def get_short_vol_per_sec(self, min_vol: float = 0.00005) -> float:
        """Returns per-second short volatility sigma (dimensionless return)."""
        return max(min_vol, math.sqrt(max(0.0, self.short_var)))

    def get_long_vol_per_sec(self, min_vol: float = 0.00005) -> float:
        """Returns per-second long volatility sigma (dimensionless return)."""
        return max(min_vol, math.sqrt(max(0.0, self.long_var)))

    def get_vol_spike_ratio(self) -> float:
        """Short-term vol / Long-term vol ratio."""
        long_v = self.get_long_vol_per_sec()
        if long_v <= 0:
            return 1.0
        return self.get_short_vol_per_sec() / long_v


class ProbabilityModel:
    """
    Brownian motion approximation for binary market outcome probability.
    P(S_T >= K | S_t) = Phi( d / (sigma * S_t * sqrt(t_left)) )
    """

    def __init__(
        self,
        platt_a: float = 1.0,
        platt_b: float = 0.0,
        fat_tail_multiplier: float = 1.15,
    ):
        self.platt_a = platt_a
        self.platt_b = platt_b
        self.fat_tail_multiplier = fat_tail_multiplier

    def compute_probability(
        self,
        current_price: float,
        strike_price_K: float,
        time_left_sec: float,
        vol_per_sec: float,
        side: str = "Up",
    ) -> ModelOutput:
        """
        Computes win probability for side ("Up" or "Down").
        """
        if current_price <= 0 or strike_price_K <= 0 or time_left_sec <= 0:
            p = 0.5
            return ModelOutput(0.0, max(0.0, time_left_sec), vol_per_sec, 0.0, p, p)

        # Lead d: positive if favored for the given side
        raw_lead = current_price - strike_price_K
        lead = raw_lead if side.lower() == "up" else -raw_lead

        # Adjust sigma for fat tails
        effective_sigma = vol_per_sec * self.fat_tail_multiplier
        # Standard deviation in dollar terms over the remaining duration:
        # sigma_dollar = effective_sigma * current_price * sqrt(t)
        sigma_dollar = effective_sigma * current_price * math.sqrt(time_left_sec)

        if sigma_dollar <= 1e-9:
            # At expiration: if lead > 0 (or == 0 on Up due to tie-breaker), win probability is 1.0
            if lead > 0 or (lead == 0 and side.lower() == "up"):
                z = 10.0
                p_raw = 1.0
            else:
                z = -10.0
                p_raw = 0.0
        else:
            z = lead / sigma_dollar
            p_raw = standard_normal_cdf(z)

        # Tie-rule adjustment: If exactly on strike, Up wins (>= rule)
        if abs(raw_lead) < 1e-6 and side.lower() == "up":
            p_raw = max(p_raw, 0.50)

        # Platt scaling on log-odds of p_raw:
        # logit(p_raw) = ln(p_raw / (1 - p_raw))
        # With default A=1.0, B=0.0, P_calibrated == p_raw
        p_safe = max(1e-7, min(1.0 - 1e-7, p_raw))
        raw_logit = math.log(p_safe / (1.0 - p_safe))
        calibrated_logit = self.platt_a * raw_logit + self.platt_b

        # Clamp logit to prevent math overflow
        calibrated_logit = max(-30.0, min(30.0, calibrated_logit))
        p_calibrated = 1.0 / (1.0 + math.exp(-calibrated_logit))

        # Clamp probabilities strictly to [0.001, 0.999]
        p_calibrated = max(0.001, min(0.999, p_calibrated))

        return ModelOutput(
            lead_usd=lead,
            time_left_sec=time_left_sec,
            vol_per_sec=vol_per_sec,
            z_score=z,
            p_raw=p_raw,
            p_calibrated=p_calibrated,
        )

    @staticmethod
    def calculate_brier_score(predictions: List[float], outcomes: List[int]) -> float:
        """
        Brier Score: Mean squared error between predicted probability and binary outcome (0 or 1).
        BS = (1 / N) * sum((P_i - Y_i)^2). Lower is better (0.0 is perfect, 0.25 is uninformative coin toss).
        """
        if not predictions or len(predictions) != len(outcomes):
            return 0.25
        return sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions)
