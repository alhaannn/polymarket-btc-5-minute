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

    def reset(self, starting_equity_usd: float = 11.0) -> None:
        """Resets the ledger and state cleanly to fresh starting equity."""
        from polybot.tz import ist_today_str
        self.ledger.reset_ledger(starting_equity_usd=starting_equity_usd, trading_day=ist_today_str())
        self.state = self.ledger.get_state()
        log.info("[CAPITAL_RESET] CapitalManager state reset to $%.2f starting equity", starting_equity_usd)

    def _initialize_starting_capital(self, starting_equity_usd: float) -> None:
        """Initializes canonical starting capital ($11.00 default = 1100 cents)."""
        from polybot.tz import ist_today_str
        cents = int(round(starting_equity_usd * 100))
        today = ist_today_str()
        self.state = CapitalState(
            total_equity_cents=cents,
            active_capital_cents=cents,
            protected_capital_cents=0,
            realized_profit_cents=0,
            realized_loss_cents=0,
            active_shortfall_cents=0,
            milestone_44_activated=False,
            daily_shutdown_status=False,
            daily_shutdown_reason="",
            daily_shutdown_trading_day="",
            daily_failed_initial_count=0,
            daily_failed_reversal_count=0,
            daily_failure_trading_day=today,
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
            trading_day=today,
            reason=f"Canonical starting capital initialized at ${starting_equity_usd:.2f}",
            idempotency_key="initial-capital-deposit",
        )
        self.ledger.save_state_and_transaction(self.state, tx)
        log.info("[CAPITAL_INITIALIZED] tx_id=%s Initialized starting capital: Active=$%.2f, Protected=$0.00, Total=$%.2f",
                 tx.tx_id, self.state.active_capital, self.state.total_equity)

    def check_day_rollover(self, current_trading_day: Optional[str] = None) -> None:
        """
        Evaluates day boundary and rolls over daily failure counters.
        If a new trading day begins, daily failure counters reset to 0.
        If a previous day's shutdown was active, it is deactivated for the new day.
        """
        today = current_trading_day or time.strftime("%Y-%m-%d")
        if self.state.daily_failure_trading_day != today:
            log.info("Day rollover detected: %s -> %s. Resetting daily failure counters.",
                     self.state.daily_failure_trading_day, today)
            self.state.daily_failed_initial_count = 0
            self.state.daily_failed_reversal_count = 0
            self.state.daily_failure_trading_day = today
            if self.state.daily_shutdown_status and self.state.daily_shutdown_trading_day != today:
                log.info("Deactivating daily shutdown from prior day %s for new day %s",
                         self.state.daily_shutdown_trading_day, today)
                self.state.daily_shutdown_status = False
                self.state.daily_shutdown_reason = ""

    def is_daily_shutdown_active(self, current_trading_day: Optional[str] = None) -> bool:
        """
        Returns True if the existing conjunctive daily shutdown is currently active
        for the given trading day.
        """
        self.check_day_rollover(current_trading_day)
        today = current_trading_day or time.strftime("%Y-%m-%d")
        return bool(
            self.state.daily_shutdown_status
            and self.state.daily_shutdown_trading_day == today
        )

    def is_trading_blocked(self, current_trading_day: Optional[str] = None) -> Tuple[bool, str]:
        """
        Returns whether new trading is currently blocked due to:
        - Daily shutdown active
        - Reconciliation mismatch detected
        - Active shortfall present
        - Depleted active capital
        """
        if self.is_daily_shutdown_active(current_trading_day):
            return True, f"Daily shutdown active: {self.state.daily_shutdown_reason}"
        if self.state.reconciliation_blocked:
            return True, f"Reconciliation discrepancy active (diff: ${self.state.last_reconciliation_diff_cents/100:.2f})"
        if self.state.active_shortfall_cents > 0:
            return True, f"Active shortfall present: ${self.state.active_shortfall:.2f}"
        if self.state.active_capital_cents <= 0:
            return True, "Active capital is depleted ($0.00)"
        return False, ""

    def verify_invariants(self) -> Tuple[bool, List[str]]:
        """
        Validates all canonical capital invariants:
        1. Active capital >= 0
        2. Protected capital >= 0
        3. Active shortfall >= 0
        4. Total equity == Active + Protected
        5. Milestone consistency (Protected == 0 before $44 milestone)
        """
        violations = []
        if self.state.active_capital_cents < 0:
            violations.append(f"Invariant 1 violated: Active capital is negative ({self.state.active_capital_cents}c)")
        if self.state.protected_capital_cents < 0:
            violations.append(f"Invariant 2 violated: Protected capital is negative ({self.state.protected_capital_cents}c)")
        if self.state.active_shortfall_cents < 0:
            violations.append(f"Invariant 3 violated: Active shortfall is negative ({self.state.active_shortfall_cents}c)")
        if self.state.total_equity_cents != (self.state.active_capital_cents + self.state.protected_capital_cents):
            violations.append(
                f"Invariant 4 violated: Total equity ({self.state.total_equity_cents}c) != "
                f"Active ({self.state.active_capital_cents}c) + Protected ({self.state.protected_capital_cents}c)"
            )
        if not self.state.milestone_44_activated and self.state.protected_capital_cents > 0:
            violations.append("Protected capital > 0 before $44 milestone activation")

        if violations:
            log.error("CAPITAL INVARIANT VIOLATION: %s", violations)
            raise ValueError(f"Capital invariant violated: {violations}")

        return True, violations

    def get_trade_budgets(self, current_trading_day: Optional[str] = None) -> Tuple[int, int]:
        """
        Calculates whole-dollar order budgets based on current active capital:
        - Initial budget = round(Active / 11)
        - Reversal budget = round((Active * 10) / 11)
        If an active shortfall exists, daily shutdown is active, or reconciliation discrepancy
        is detected, returns 0, 0 (trading halted).
        """
        blocked, reason = self.is_trading_blocked(current_trading_day)
        if blocked:
            log.warning("Trade budgets set to $0: %s", reason)
            return 0, 0

        active_usd = self.state.active_capital
        initial_raw = active_usd / 11.0
        reversal_raw = (active_usd * 10.0) / 11.0

        initial_budget = round_half_up_to_whole_dollar(initial_raw)
        reversal_budget = round_half_up_to_whole_dollar(reversal_raw)

        log.debug("[TRADE_BUDGET_CALCULATED] Initial=$%d, Reversal=$%d (Active=$%.2f)",
                  initial_budget, reversal_budget, active_usd)
        return initial_budget, reversal_budget

    def process_settlement(
        self,
        settlement_id: str,
        net_pnl_usd: float,
        fee_usd: float = 0.0,
        trading_day: Optional[str] = None,
        signal_type: Optional[str] = None,
    ) -> CapitalState:
        """
        Applies settlement net P&L to capital pools according to canonical rules.
        Idempotent: Duplicate calls for the same settlement_id are safely ignored.

        Odd-Cent Allocation Policy:
        When splitting an odd number of cents, `cents // 2` is assigned to Active
        and `cents - (cents // 2)` to Protected.

        Active Shortfall Handling:
        If a realized loss exceeds active capital, active capital is floored at 0,
        the shortfall is recorded in `active_shortfall_cents`, and protected capital
        is strictly untouched.

        Phase 5 Daily Shutdown:
        If a trade fails (net_pnl_cents < 0) and both failed_initial_count >= 1
        and failed_reversal_count >= 1 on the same trading day, triggers the existing
        conjunctive shutdown rule.
        """
        # 1. Idempotency check
        if self.ledger.is_settlement_processed(settlement_id):
            log.warning("Settlement %s already processed. Skipping duplicate.", settlement_id)
            return self.state

        day_key = trading_day or time.strftime("%Y-%m-%d")
        self.check_day_rollover(day_key)
        net_pnl_cents = int(round(net_pnl_usd * 100))
        fee_cents = int(round(fee_usd * 100))

        active_before = self.state.active_capital_cents
        protected_before = self.state.protected_capital_cents
        total_before = self.state.total_equity_cents

        tx_list: List[CapitalTransaction] = []

        if not self.state.milestone_44_activated:
            # --- PRE-MILESTONE ACCOUNTING ---
            if net_pnl_cents >= 0:
                self.state.realized_profit_cents += net_pnl_cents

                # Recover any active shortfall first
                if self.state.active_shortfall_cents > 0:
                    offset = min(net_pnl_cents, self.state.active_shortfall_cents)
                    self.state.active_shortfall_cents -= offset
                    remaining_profit = net_pnl_cents - offset
                    log.info("Settlement %s: Recovered %sc of active shortfall. Remaining shortfall: %sc",
                             settlement_id, offset, self.state.active_shortfall_cents)
                else:
                    remaining_profit = net_pnl_cents

                self.state.active_capital_cents += remaining_profit
                self.state.total_equity_cents = self.state.active_capital_cents + self.state.protected_capital_cents

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
                    reason=f"Pre-milestone settlement profit: +${net_pnl_usd:.2f}",
                    idempotency_key=f"settle-{settlement_id}",
                )
                tx_list.append(tx_pnl)

                # Check for $44 Milestone Trigger ($44.00 = 4400 cents)
                if self.state.total_equity_cents >= 4400:
                    mid_active_before = self.state.active_capital_cents
                    mid_prot_before = self.state.protected_capital_cents
                    mid_tot_before = self.state.total_equity_cents

                    # 50/50 split of eligible equity (odd-cent policy: protected gets remainder)
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
                    log.info("[MILESTONE_44_ACTIVATED] tx_id=%s 🎉 $44 MILESTONE ACTIVATED! Active=$%.2f, Protected=$%.2f, Total=$%.2f",
                             tx_milestone.tx_id, self.state.active_capital, self.state.protected_capital, self.state.total_equity)

            else:
                # Pre-milestone loss
                self.state.realized_loss_cents += abs(net_pnl_cents)
                new_active = active_before + net_pnl_cents
                if new_active < 0:
                    shortfall = abs(new_active)
                    self.state.active_shortfall_cents += shortfall
                    self.state.active_capital_cents = 0
                    self.state.total_equity_cents = 0
                    log.critical("ACCOUNTING SHORTFALL: Pre-milestone loss %sc exceeds active capital %sc! Shortfall is %sc.",
                                 abs(net_pnl_cents), active_before, self.state.active_shortfall_cents)
                else:
                    self.state.active_capital_cents = new_active
                    self.state.total_equity_cents = new_active

                tx_loss = CapitalTransaction(
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
                    reason=f"Pre-milestone settlement loss: -${abs(net_pnl_usd):.2f}",
                    idempotency_key=f"settle-{settlement_id}",
                )
                tx_list.append(tx_loss)

        else:
            # --- POST-MILESTONE ACCOUNTING ---
            if net_pnl_cents >= 0:
                self.state.realized_profit_cents += net_pnl_cents

                # Recover any active shortfall first before compounding
                if self.state.active_shortfall_cents > 0:
                    offset = min(net_pnl_cents, self.state.active_shortfall_cents)
                    self.state.active_shortfall_cents -= offset
                    rem_profit = net_pnl_cents - offset

                    tx_rec = CapitalTransaction(
                        tx_id=f"tx-rec-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}",
                        event_type=TransactionType.SHORTFALL_RECOVERY,
                        amount_cents=offset,
                        active_before_cents=active_before,
                        active_after_cents=active_before,
                        protected_before_cents=protected_before,
                        protected_after_cents=protected_before,
                        total_before_cents=total_before,
                        total_after_cents=total_before,
                        fee_cents=0,
                        settlement_id=settlement_id,
                        trading_day=day_key,
                        reason=f"Shortfall recovery: ${offset/100:.2f} profit absorbed existing active deficit",
                        idempotency_key=f"shortfall-recovery-{settlement_id}",
                    )
                    tx_list.append(tx_rec)
                    log.info("Settlement %s: Recovered %sc of active shortfall. Remaining shortfall: %sc",
                             settlement_id, offset, self.state.active_shortfall_cents)
                else:
                    rem_profit = net_pnl_cents

                # 50% Active / 50% Protected allocation on remaining profit
                active_share = rem_profit // 2
                protected_share = rem_profit - active_share

                self.state.active_capital_cents += active_share
                self.state.protected_capital_cents += protected_share
                self.state.total_equity_cents = self.state.active_capital_cents + self.state.protected_capital_cents

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
                log.info("[PROFIT_ALLOCATED] tx_id=%s Settlement=%s Profit=+$%.2f | Active=$%.2f, Protected=$%.2f, Total=$%.2f",
                         tx_profit.tx_id, settlement_id, net_pnl_usd, self.state.active_capital, self.state.protected_capital, self.state.total_equity)

            else:
                # Realized losses deduct from active capital ONLY
                self.state.realized_loss_cents += abs(net_pnl_cents)
                new_active = active_before + net_pnl_cents

                if new_active < 0:
                    shortfall = abs(new_active)
                    self.state.active_shortfall_cents += shortfall
                    self.state.active_capital_cents = 0
                    self.state.total_equity_cents = self.state.protected_capital_cents
                    log.critical(
                        "ACCOUNTING SHORTFALL: Post-milestone loss %sc exceeds active capital %sc by %sc! "
                        "Total shortfall is %sc. Protected capital ($%.2f) remains isolated. Trading halted.",
                        abs(net_pnl_cents), active_before, shortfall,
                        self.state.active_shortfall_cents, self.state.protected_capital
                    )
                else:
                    self.state.active_capital_cents = new_active
                    self.state.total_equity_cents = self.state.active_capital_cents + self.state.protected_capital_cents

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
                log.info("[LOSS_DEDUCTED] tx_id=%s Settlement=%s Loss=-$%.2f | Active=$%.2f, Protected=$%.2f, Total=$%.2f",
                         tx_loss.tx_id, settlement_id, abs(net_pnl_usd), self.state.active_capital, self.state.protected_capital, self.state.total_equity)

        # Daily Failure Tracking & Conjunctive Shutdown Trigger (Roadmap Phase 5)
        if net_pnl_cents < 0 and signal_type:
            sig_upper = signal_type.upper()
            if sig_upper == "INITIAL":
                self.state.daily_failed_initial_count += 1
                log.info("Recorded failed INITIAL trade #%d for day %s",
                         self.state.daily_failed_initial_count, day_key)
            elif sig_upper == "REVERSAL":
                self.state.daily_failed_reversal_count += 1
                log.info("Recorded failed REVERSAL trade #%d for day %s",
                         self.state.daily_failed_reversal_count, day_key)

            # Check existing conjunctive condition:
            # failed_initial_count >= 1 AND failed_reversal_count >= 1 on same day
            if (self.state.daily_failed_initial_count >= 1 and
                self.state.daily_failed_reversal_count >= 1 and
                not self.state.daily_shutdown_status):
                shutdown_reason = (
                    f"Conjunctive failure triggered: {self.state.daily_failed_initial_count} initial and "
                    f"{self.state.daily_failed_reversal_count} reversal trade(s) failed on {day_key}"
                )
                self.record_daily_shutdown_qualifying_event(
                    trading_day=day_key,
                    failed_initial_count=self.state.daily_failed_initial_count,
                    failed_reversal_count=self.state.daily_failed_reversal_count,
                    reason=shutdown_reason,
                )

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
        log.warning("[DAILY_SHUTDOWN_TRIGGERED] event_id=%s for %s: %s", event_id, trading_day, reason)
        log.info("[NEXT_DAY_ACTIVATION_ELIGIBLE] event_id=%s Marked eligible for Option B reserve activation", event_id)
        return event_id

    def check_and_activate_next_day_reserve(
        self,
        current_trading_day: Optional[str] = None,
    ) -> Tuple[bool, float, str]:
        """
        Option B Next-Day Reserve Release (Roadmap Phase 6):
        If a prior day triggered the daily shutdown, releases 50% of current protected capital
        and ADDS it to existing active capital. Total equity remains identical.
        Returns: (activated: bool, released_usd: float, message: str)
        """
        today = current_trading_day or time.strftime("%Y-%m-%d")
        self.check_day_rollover(today)

        unactivated_event = self.ledger.get_unactivated_shutdown_event()
        if not unactivated_event:
            self.state.next_day_activation_eligible = False
            return False, 0.0, "No qualifying shutdown event eligible for reserve release"

        shutdown_day = unactivated_event["trading_day"]
        event_id = unactivated_event["event_id"]

        if today <= shutdown_day:
            return False, 0.0, f"Cannot activate reserve on or before shutdown day ({shutdown_day})"

        if self.state.protected_capital_cents <= 0:
            self.state.next_day_activation_eligible = False
            self.ledger.mark_shutdown_event_activated(event_id, activated_at_ts=time.time())
            return False, 0.0, "Protected capital is zero; no reserve available to release"

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
        self.state.last_activation_trading_day = today
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
            trading_day=today,
            reason=f"Option B: Released 50% protected capital (${released_cents/100:.2f}) into active capital for new day {today}",
            idempotency_key=f"reserve-release-{event_id}",
        )

        # Atomic commit: state update + transaction audit + shutdown event marked activated
        self.ledger.save_state_and_transaction(
            self.state,
            tx,
            activate_shutdown_event_id=event_id,
        )
        self.verify_invariants()

        released_usd = round(released_cents / 100.0, 2)
        log.info("[PROTECTED_CAPITAL_RELEASED] tx_id=%s Option B: Released $%.2f. New Active=$%.2f, Remaining Protected=$%.2f",
                 tx.tx_id, released_usd, self.state.active_capital, self.state.protected_capital)

        return True, released_usd, f"Released ${released_usd:.2f} protected capital into active"
