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
import math
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
        sizing_mode: str = "canonical",
    ):
        self.min_order_usd = min_order_usd
        self.taker_fee_rate = taker_fee_rate
        self.sizing_mode = sizing_mode

    def calculate_initial_budget(self, active_capital_usd: float) -> int:
        """Derives whole-dollar Initial order budget."""
        if active_capital_usd <= 0:
            return 0
        if self.sizing_mode == "tiered":
            if active_capital_usd < 15.0:
                return 2  # Boosted early compounding on $11 balance
            elif active_capital_usd < 25.0:
                return 3
            elif active_capital_usd < 35.0:
                return 4
            elif active_capital_usd < 50.0:
                return 5
            else:
                return min(8, max(1, int(round(active_capital_usd * 0.15))))
        else:
            raw = active_capital_usd / 11.0
            return round_half_up_to_whole_dollar(raw)

    def calculate_reversal_budget(self, active_capital_usd: float) -> int:
        """Derives whole-dollar Reversal order budget."""
        if active_capital_usd <= 0:
            return 0
        if self.sizing_mode == "tiered":
            init_b = self.calculate_initial_budget(active_capital_usd)
            return max(1, int(round(init_b * 1.5)))
        else:
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

    def get_executable_reversal_budget(
        self,
        active_capital_usd: float,
        open_exposure_usd: float = 0.0,
    ) -> int:
        """
        Derives executable whole-dollar reversal budget fitting within free active funds.
        Prevents budget collisions when initial order is open and fees apply.
        """
        if active_capital_usd <= 0:
            return 0
        canonical_rev = self.calculate_reversal_budget(active_capital_usd)
        free_active = max(0.0, active_capital_usd - open_exposure_usd)
        max_affordable = math.floor(free_active / (1.0 + self.taker_fee_rate))
        return int(max(0, min(canonical_rev, max_affordable)))

    def validate_order_budget(
        self,
        signal_type: str,
        active_capital_usd: float,
        open_exposure_usd: float = 0.0,
        override_budget_usd: Optional[float] = None,
        allow_adaptive_clamping: bool = False,
    ) -> BudgetValidationResult:
        """
        Validates whether a proposed order can be safely funded by active capital.
        - Computes free active funds after subtracting open exposure.
        - Validates against minimum order size and fee requirements.
        - Supports adaptive whole-dollar clamping to prevent order rejections when
          available funds are slightly below the canonical budget (e.g. due to fees or rounding).
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
            # If adaptive clamping is enabled, attempt to downsize whole-dollar budget to available free funds
            if allow_adaptive_clamping:
                max_affordable = math.floor(free_active / (1.0 + self.taker_fee_rate))
                if max_affordable >= self.min_order_usd:
                    approved_budget = float(max_affordable)
                    clamped_fee = round(approved_budget * self.taker_fee_rate, 4)
                    log.info(
                        "[BUDGET_ADAPTED] %s order budget clamped to affordable free active funds: "
                        "approved=$%.2f (calc=$%.2f, fee=$%.3f, free_active=$%.2f)",
                        signal_type, approved_budget, calc_budget, clamped_fee, free_active,
                    )
                    return BudgetValidationResult(
                        allowed=True,
                        approved_budget_usd=approved_budget,
                        calculated_budget_usd=calc_budget,
                        free_active_usd=free_active,
                        fee_estimate_usd=clamped_fee,
                        reason=(
                            f"Clamped to available free active balance: ${approved_budget:.2f} "
                            f"(originally ${calc_budget:.2f})."
                        ),
                    )

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
