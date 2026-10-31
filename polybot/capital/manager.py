"""
Core Capital Management & Compounding Engine.
Implements canonical allocation rules:
- $11 starting capital
- 1:10 initial vs reversal sizing ratio
- $44 milestone 50/50 split
- Post-milestone 50/50 profit compounding / active-only loss deduction
- Option B next-day 50% protected reserve activation
- Strict invariant validation and exact-once idempotency
"""
from __future__ import annotations

import decimal
import logging
import math
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polybot.capital.ledger import CapitalLedger
from polybot.capital.models import CapitalState, CapitalTransaction, TransactionType

log = logging.getLogger("polybot.capital.manager")


def round_half_up_to_whole_dollar(val: float | decimal.Decimal) -> int:
    """Deterministic rounding: exact half-dollar ties round upward."""
    d = decimal.Decimal(str(val))
    return int(d.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


class CapitalManager:
    """Authoritative capital management engine governing equity, reserves, and sizing."""

    def __init__(
        self,
        ledger: Optional[CapitalLedger] = None,
        db_path: Path | str = Path("data/paper_ledger.db"),
        starting_equity_usd: float = 11.0,
    ):
        self.ledger = ledger or CapitalLedger(db_path)
        self.state = self.ledger.get_state()
        if self.state is None:
            self._initialize_starting_capital(starting_equity_usd)
        else:
            self.verify_invariants()

    def _initialize_starting_capital(self, starting_equity_usd: float) -> None:
        """Initializes canonical starting capital ($11.00 default = 1100 cents)."""
        cents = int(round(starting_equity_usd * 100))
        self.state = CapitalState(
            total_equity_cents=cents,
            active_capital_cents=cents,
            protected_capital_cents=0,
            realized_profit_cents=0,
            realized_loss_cents=0,
            milestone_44_activated=False,
            daily_shutdown_status=False,
            daily_shutdown_reason="",
            daily_shutdown_trading_day="",
            next_day_activation_eligible=False,
            updated_at_ts=time.time(),
        )
        tx = CapitalTransaction(
            tx_id=f"tx-init-{int(time.time() * 1000)}",
            event_type=TransactionType.INITIAL_CAPITAL,
            amount_cents=cents,
            active_before_cents=0,
            active_after_cents=cents,
            protected_before_cents=0,
            protected_after_cents=0,
            total_before_cents=0,
            total_after_cents=cents,
            fee_cents=0,
            trading_day=time.strftime("%Y-%m-%d"),
            reason=f"Canonical starting capital initialized at ${starting_equity_usd:.2f}",
            idempotency_key="initial-capital-deposit",
        )
        self.ledger.save_state_and_transaction(self.state, tx)
        log.info("Initialized starting capital: Active=$%.2f, Protected=$0.00, Total=$%.2f",
                 self.state.active_capital, self.state.total_equity)

    def verify_invariants(self) -> Tuple[bool, List[str]]:
        """
        Validates all canonical capital invariants:
        1. Active capital >= 0
        2. Protected capital >= 0
        3. Total equity == Active + Protected
        4. Milestone consistency
        """
        violations = []
        if self.state.active_capital_cents < 0:
            violations.append(f"Invariant 1 violated: Active capital is negative ({self.state.active_capital_cents}c)")
        if self.state.protected_capital_cents < 0:
            violations.append(f"Invariant 2 violated: Protected capital is negative ({self.state.protected_capital_cents}c)")
        if self.state.total_equity_cents != (self.state.active_capital_cents + self.state.protected_capital_cents):
            violations.append(
                f"Invariant 3 violated: Total equity ({self.state.total_equity_cents}c) != "
                f"Active ({self.state.active_capital_cents}c) + Protected ({self.state.protected_capital_cents}c)"
            )
        if not self.state.milestone_44_activated and self.state.protected_capital_cents > 0:
            violations.append("Protected capital > 0 before $44 milestone activation")

        if violations:
            log.error("CAPITAL INVARIANT VIOLATION: %s", violations)
            raise ValueError(f"Capital invariant violated: {violations}")

        return True, violations

    def get_trade_budgets(self) -> Tuple[int, int]:
        """
        Calculates whole-dollar order budgets based on current active capital:
        - Initial budget = round(Active / 11)
        - Reversal budget = round((Active * 10) / 11)
        """
        active_usd = self.state.active_capital
        if active_usd <= 0.0:
            return 0, 0

        initial_raw = active_usd / 11.0
        reversal_raw = (active_usd * 10.0) / 11.0

        initial_budget = round_half_up_to_whole_dollar(initial_raw)
        reversal_budget = round_half_up_to_whole_dollar(reversal_raw)

        return initial_budget, reversal_budget

    def process_settlement(
        self,
        settlement_id: str,
        net_pnl_usd: float,
        fee_usd: float = 0.0,
        trading_day: Optional[str] = None,
    ) -> CapitalState:
        """
        Applies settlement P&L to capital pools according to canonical rules.
        Idempotent: Duplicate calls for the same settlement_id are safely ignored.
        """
        # 1. Idempotency check
        if self.ledger.is_settlement_processed(settlement_id):
            log.warning("Settlement %s already processed. Skipping duplicate.", settlement_id)
            return self.state

        day_key = trading_day or time.strftime("%Y-%m-%d")
        net_pnl_cents = int(round(net_pnl_usd * 100))
        fee_cents = int(round(fee_usd * 100))

        active_before = self.state.active_capital_cents
        protected_before = self.state.protected_capital_cents
        total_before = self.state.total_equity_cents

        tx_list: List[CapitalTransaction] = []

        if not self.state.milestone_44_activated:
            # --- PRE-MILESTONE ACCOUNTING ---
            new_active = active_before + net_pnl_cents
            new_protected = protected_before  # 0
            new_total = new_active + new_protected

            self.state.active_capital_cents = max(0, new_active)
            self.state.total_equity_cents = max(0, new_total)

            if net_pnl_cents >= 0:
                self.state.realized_profit_cents += net_pnl_cents
            else:
                self.state.realized_loss_cents += abs(net_pnl_cents)

            tx_pnl = CapitalTransaction(
                tx_id=f"tx-pnl-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
                event_type=TransactionType.PRE_MILESTONE_PNL,
                amount_cents=net_pnl_cents,
                active_before_cents=active_before,
                active_after_cents=self.state.active_capital_cents,
                protected_before_cents=protected_before,
                protected_after_cents=protected_before,
                total_before_cents=total_before,
                total_after_cents=self.state.total_equity_cents,
                fee_cents=fee_cents,
                settlement_id=settlement_id,
                trading_day=day_key,
                reason=f"Pre-milestone settlement PnL: ${net_pnl_usd:+.2f}",
                idempotency_key=f"settle-{settlement_id}",
            )
            tx_list.append(tx_pnl)

            # Check for $44 Milestone Trigger ($44.00 = 4400 cents)
            if self.state.total_equity_cents >= 4400:
                mid_active_before = self.state.active_capital_cents
                mid_prot_before = self.state.protected_capital_cents
                mid_tot_before = self.state.total_equity_cents

                # 50/50 split of eligible equity
                split_active = mid_tot_before // 2
                split_protected = mid_tot_before - split_active

                self.state.active_capital_cents = split_active
                self.state.protected_capital_cents = split_protected
                self.state.milestone_44_activated = True

                tx_milestone = CapitalTransaction(
                    tx_id=f"tx-ms44-{int(time.time() * 1000)}",
                    event_type=TransactionType.MILESTONE_44_ACTIVATION,
                    amount_cents=mid_tot_before,
                    active_before_cents=mid_active_before,
                    active_after_cents=split_active,
                    protected_before_cents=mid_prot_before,
                    protected_after_cents=split_protected,
                    total_before_cents=mid_tot_before,
                    total_after_cents=mid_tot_before,
                    fee_cents=0,
                    settlement_id=settlement_id,
                    trading_day=day_key,
                    reason=f"Reached $44 milestone: Split ${mid_tot_before/100:.2f} into Active=${split_active/100:.2f}, Protected=${split_protected/100:.2f}",
                    idempotency_key=f"milestone-44-{settlement_id}",
                )
                tx_list.append(tx_milestone)
                log.info("🎉 $44 MILESTONE ACTIVATED! Active=$%.2f, Protected=$%.2f, Total=$%.2f",
                         self.state.active_capital, self.state.protected_capital, self.state.total_equity)

        else:
            # --- POST-MILESTONE ACCOUNTING ---
            if net_pnl_cents >= 0:
                # 50% Active / 50% Protected allocation
                active_share = net_pnl_cents // 2
                protected_share = net_pnl_cents - active_share

                self.state.active_capital_cents += active_share
                self.state.protected_capital_cents += protected_share
                self.state.total_equity_cents += net_pnl_cents
                self.state.realized_profit_cents += net_pnl_cents

                tx_profit = CapitalTransaction(
                    tx_id=f"tx-prof-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
                    event_type=TransactionType.PROFIT_ALLOCATION,
                    amount_cents=net_pnl_cents,
                    active_before_cents=active_before,
                    active_after_cents=self.state.active_capital_cents,
                    protected_before_cents=protected_before,
                    protected_after_cents=self.state.protected_capital_cents,
                    total_before_cents=total_before,
                    total_after_cents=self.state.total_equity_cents,
                    fee_cents=fee_cents,
                    settlement_id=settlement_id,
                    trading_day=day_key,
                    reason=f"Post-milestone profit +${net_pnl_usd:.2f}: +${active_share/100:.2f} active, +${protected_share/100:.2f} protected",
                    idempotency_key=f"settle-{settlement_id}",
                )
                tx_list.append(tx_profit)
            else:
                # Realized losses deduct from active capital ONLY
                self.state.active_capital_cents = max(0, self.state.active_capital_cents + net_pnl_cents)
                self.state.total_equity_cents = self.state.active_capital_cents + self.state.protected_capital_cents
                self.state.realized_loss_cents += abs(net_pnl_cents)

                tx_loss = CapitalTransaction(
                    tx_id=f"tx-loss-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
                    event_type=TransactionType.LOSS_DEDUCTION,
                    amount_cents=net_pnl_cents,
                    active_before_cents=active_before,
                    active_after_cents=self.state.active_capital_cents,
                    protected_before_cents=protected_before,
                    protected_after_cents=protected_before,
                    total_before_cents=total_before,
                    total_after_cents=self.state.total_equity_cents,
                    fee_cents=fee_cents,
                    settlement_id=settlement_id,
                    trading_day=day_key,
                    reason=f"Post-milestone loss -${abs(net_pnl_usd):.2f}: deducted from active only",
                    idempotency_key=f"settle-{settlement_id}",
                )
                tx_list.append(tx_loss)

        self.state.last_processed_settlement_id = settlement_id
        self.verify_invariants()

        # Atomically save all changes to SQLite
        settlement_record = {
            "settlement_id": settlement_id,
            "net_pnl_cents": net_pnl_cents,
            "fee_cents": fee_cents,
        }
        for tx in tx_list:
            self.ledger.save_state_and_transaction(self.state, tx, processed_settlement=settlement_record)

        return self.state

    def record_daily_shutdown_qualifying_event(
        self,
        trading_day: str,
        failed_initial_count: int,
        failed_reversal_count: int,
        reason: str = "Conjunctive failure: at least 1 Initial and 1 Reversal failed on same day",
    ) -> Optional[str]:
        """
        Records the existing daily shutdown condition firing.
        Persists qualification for Option B next-day protected reserve activation.
        """
        event_id = f"shutdown-{trading_day}"
        self.state.daily_shutdown_status = True
        self.state.daily_shutdown_reason = reason
        self.state.daily_shutdown_trading_day = trading_day
        self.state.next_day_activation_eligible = True

        shutdown_record = {
            "event_id": event_id,
            "trading_day": trading_day,
            "failed_initial_count": failed_initial_count,
            "failed_reversal_count": failed_reversal_count,
            "activated": False,
        }

        tx = CapitalTransaction(
            tx_id=f"tx-shut-{int(time.time() * 1000)}",
            event_type=TransactionType.INITIAL_CAPITAL,  # Audit transaction
            amount_cents=0,
            active_before_cents=self.state.active_capital_cents,
            active_after_cents=self.state.active_capital_cents,
            protected_before_cents=self.state.protected_capital_cents,
            protected_after_cents=self.state.protected_capital_cents,
            total_before_cents=self.state.total_equity_cents,
            total_after_cents=self.state.total_equity_cents,
            fee_cents=0,
            trading_day=trading_day,
            reason=f"DAILY SHUTDOWN: {reason}. Eligible for next-day 50% reserve activation.",
            idempotency_key=f"shutdown-event-{trading_day}",
        )
        self.ledger.save_state_and_transaction(self.state, tx, shutdown_event=shutdown_record)
        log.warning("DAILY SHUTDOWN RECORDED for %s: %s (Eligible for next-day reserve release)", trading_day, reason)
        return event_id

    def check_and_activate_next_day_reserve(
        self,
        current_trading_day: str,
    ) -> Tuple[bool, float, str]:
        """
        Option B Next-Day Reserve Release:
        If the prior day triggered the daily shutdown, releases 50% of current protected capital
        and ADDS it to existing active capital. Total equity remains identical.
        Returns: (activated: bool, released_usd: float, message: str)
        """
        if not self.state.next_day_activation_eligible:
            return False, 0.0, "No qualifying shutdown event eligible for reserve release"

        if self.state.daily_shutdown_trading_day == current_trading_day:
            return False, 0.0, "Cannot activate reserve on the same trading day as the shutdown"

        if self.state.protected_capital_cents <= 0:
            self.state.next_day_activation_eligible = False
            return False, 0.0, "Protected capital is zero; no reserve available to release"

        unactivated_event = self.ledger.get_unactivated_shutdown_event()
        if not unactivated_event:
            self.state.next_day_activation_eligible = False
            return False, 0.0, "No unactivated shutdown event record found"

        event_id = unactivated_event["event_id"]

        # Calculate exact 50% release of current protected capital
        protected_before = self.state.protected_capital_cents
        active_before = self.state.active_capital_cents
        total_before = self.state.total_equity_cents

        released_cents = protected_before // 2
        new_protected = protected_before - released_cents
        new_active = active_before + released_cents

        self.state.active_capital_cents = new_active
        self.state.protected_capital_cents = new_protected
        self.state.next_day_activation_eligible = False
        self.state.last_activated_shutdown_event_id = event_id
        self.state.last_activation_trading_day = current_trading_day
        self.state.daily_shutdown_status = False  # Reset shutdown for new day
        self.state.daily_shutdown_reason = ""

        tx = CapitalTransaction(
            tx_id=f"tx-rel-{int(time.time() * 1000)}",
            event_type=TransactionType.RESERVE_RELEASE_OPTION_B,
            amount_cents=released_cents,
            active_before_cents=active_before,
            active_after_cents=new_active,
            protected_before_cents=protected_before,
            protected_after_cents=new_protected,
            total_before_cents=total_before,
            total_after_cents=total_before,  # Total equity unchanged!
            fee_cents=0,
            trading_day=current_trading_day,
            reason=f"Option B: Released 50% protected capital (${released_cents/100:.2f}) into active capital for new day {current_trading_day}",
            idempotency_key=f"reserve-release-{event_id}",
        )

        now = time.time()
        self.ledger.mark_shutdown_event_activated(event_id, activated_at_ts=now)
        self.ledger.save_state_and_transaction(self.state, tx)
        self.verify_invariants()

        released_usd = round(released_cents / 100.0, 2)
        log.info("RESERVE RELEASE OPTION B EXECUTED: Released $%.2f. New Active=$%.2f, Remaining Protected=$%.2f",
                 released_usd, self.state.active_capital, self.state.protected_capital)

        return True, released_usd, f"Released ${released_usd:.2f} protected capital into active"
