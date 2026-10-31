"""
Dynamic Trade-Sizing Engine for 5-minute Reversal Strategy (Roadmap Phase 3).
Derives exact whole-dollar order budgets from Active Capital:
- Initial budget = round_half_up(Active / 11)
- Reversal budget = round_half_up((Active * 10) / 11)
Validates execution safety: free active funds, open collateral, fees, and minimum order bounds.
Protected capital is strictly isolated and can never fund orders.
"""
from __future__ import annotations

import decimal
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger("polybot.capital.sizing")


def round_half_up_to_whole_dollar(val: float | decimal.Decimal) -> int:
    """
    Deterministic decimal rounding:
    Rounds to nearest whole dollar; exact half-dollar ties (.50) round upward.
    """
    if isinstance(val, (int, float)):
        # Convert float to string first to avoid binary float precision quirks
        d = decimal.Decimal(str(round(val, 6)))
    else:
        d = val
    return int(d.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


@dataclass
class BudgetValidationResult:
    allowed: bool
    approved_budget_usd: float
    calculated_budget_usd: float
    free_active_usd: float
    fee_estimate_usd: float
    reason: str


class DynamicTradeBudgetCalculator:
    """
    Calculates and validates dynamic order budgets from active capital.
    Strictly isolates protected capital.
    """

    def __init__(
        self,
        min_order_usd: float = 1.0,
        taker_fee_rate: float = 0.001,
    ):
        self.min_order_usd = min_order_usd
        self.taker_fee_rate = taker_fee_rate

    def calculate_initial_budget(self, active_capital_usd: float) -> int:
        """Derives whole-dollar Initial order budget = round_half_up(Active / 11)."""
        if active_capital_usd <= 0:
            return 0
        raw = active_capital_usd / 11.0
        return round_half_up_to_whole_dollar(raw)

    def calculate_reversal_budget(self, active_capital_usd: float) -> int:
        """Derives whole-dollar Reversal order budget = round_half_up((Active * 10) / 11)."""
        if active_capital_usd <= 0:
            return 0
        raw = (active_capital_usd * 10.0) / 11.0
        return round_half_up_to_whole_dollar(raw)

    def get_budgets(self, active_capital_usd: float) -> Tuple[int, int]:
        """Returns (initial_budget, reversal_budget) in whole dollars."""
        if active_capital_usd < 0:
            raise ValueError(f"Active capital cannot be negative: ${active_capital_usd:.2f}")
        return (
            self.calculate_initial_budget(active_capital_usd),
            self.calculate_reversal_budget(active_capital_usd),
        )

    def validate_order_budget(
        self,
        signal_type: str,
        active_capital_usd: float,
        open_exposure_usd: float = 0.0,
        override_budget_usd: Optional[float] = None,
    ) -> BudgetValidationResult:
        """
        Validates whether a proposed order can be safely funded by active capital.
        - Computes free active funds after subtracting open exposure.
        - Validates against minimum order size and fee requirements.
        - Ensures protected capital is completely untouched.
        """
        if active_capital_usd < 0:
            raise ValueError(f"Active capital cannot be negative: ${active_capital_usd:.2f}")

        # 1. Determine free active capital (cannot spend collateral committed to open positions)
        free_active = max(0.0, round(active_capital_usd - open_exposure_usd, 4))

        # 2. Derive budget based on signal type
        sig_upper = signal_type.upper()
        if override_budget_usd is not None and override_budget_usd > 0:
            calc_budget = float(round_half_up_to_whole_dollar(override_budget_usd))
        elif sig_upper == "INITIAL":
            calc_budget = float(self.calculate_initial_budget(active_capital_usd))
        elif sig_upper == "REVERSAL":
            calc_budget = float(self.calculate_reversal_budget(active_capital_usd))
        else:
            return BudgetValidationResult(
                allowed=False,
                approved_budget_usd=0.0,
                calculated_budget_usd=0.0,
                free_active_usd=free_active,
                fee_estimate_usd=0.0,
                reason=f"Unknown signal type: '{signal_type}'. Must be 'INITIAL' or 'REVERSAL'.",
            )

        fee_est = round(calc_budget * self.taker_fee_rate, 4)
        total_required = round(calc_budget + fee_est, 4)

        # 3. Check active capital availability
        if active_capital_usd <= 0.0:
            return BudgetValidationResult(
                allowed=False,
                approved_budget_usd=0.0,
                calculated_budget_usd=calc_budget,
                free_active_usd=free_active,
                fee_estimate_usd=fee_est,
                reason="Active capital is zero or depleted. Order blocked.",
            )

        # 4. Check minimum order constraint
        if calc_budget < self.min_order_usd:
            return BudgetValidationResult(
                allowed=False,
                approved_budget_usd=0.0,
                calculated_budget_usd=calc_budget,
                free_active_usd=free_active,
                fee_estimate_usd=fee_est,
                reason=f"Calculated budget ${calc_budget:.2f} is below minimum allowed order ${self.min_order_usd:.2f}.",
            )

        # 5. Check free active funds sufficiency (including fees)
        if total_required > free_active:
            return BudgetValidationResult(
                allowed=False,
                approved_budget_usd=0.0,
                calculated_budget_usd=calc_budget,
                free_active_usd=free_active,
                fee_estimate_usd=fee_est,
                reason=(
                    f"Insufficient free active funds: ${free_active:.2f} free < "
                    f"${total_required:.2f} required (${calc_budget:.2f} + ${fee_est:.3f} fee). "
                    f"Open exposure: ${open_exposure_usd:.2f}."
                ),
            )

        log.debug(
            "Budget validated for %s: approved=$%.2f (calc=$%.2f, fee=$%.3f, free=$%.2f)",
            signal_type, calc_budget, calc_budget, fee_est, free_active
        )
        return BudgetValidationResult(
            allowed=True,
            approved_budget_usd=calc_budget,
            calculated_budget_usd=calc_budget,
            free_active_usd=free_active,
            fee_estimate_usd=fee_est,
            reason="OK",
        )
