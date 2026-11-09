"""
Compartmentalized Market Cell Engine.
Governs isolated capital silos ($12 initial per asset),
independent compounding, reversal wipeout bulkheads, and autonomous
self-healing reviver dividend payouts.
"""
from __future__ import annotations

import decimal
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("polybot.capital.cell")


def round_half_up_int(val: float | decimal.Decimal) -> int:
    d = decimal.Decimal(str(val))
    return int(d.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


@dataclass
class HarvestEvent:
    timestamp_ts: float
    winning_asset: str
    target_capital_usd: float
    self_seed_usd: float
    halted_revived_assets: List[str]
    revival_funds_usd: float
    sweep_amount_usd: float
    sweep_tx_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_ts": self.timestamp_ts,
            "winning_asset": self.winning_asset.upper(),
            "target_capital_usd": self.target_capital_usd,
            "self_seed_usd": self.self_seed_usd,
            "halted_revived_assets": [a.upper() for a in self.halted_revived_assets],
            "revival_funds_usd": self.revival_funds_usd,
            "sweep_amount_usd": self.sweep_amount_usd,
            "sweep_tx_hash": self.sweep_tx_hash,
        }


class MarketCell:
    """
    Isolated capital cell dedicated to a single market asset.
    Strictly walls off capital from other cells to prevent cross-asset contagion.
    """

    def __init__(
        self,
        asset: str,
        initial_capital_cents: int = 1200,  # $12.00 starting allocation
        target_milestone_cents: int = 10000,  # $100.00 graduation milestone
        max_reversal_stake_cents: int = 7500,  # $75.00 CLOB taker limit
    ):
        self.asset = asset.lower().strip()
        self.capital_cents = initial_capital_cents
        self.initial_capital_cents = initial_capital_cents
        self.target_milestone_cents = target_milestone_cents
        self.max_reversal_stake_cents = max_reversal_stake_cents

        self.status = "ACTIVE"  # ACTIVE, TRIGGER_ZONE, POSITION_ACTIVE, REVERSAL_ACTIVE, HALTED_DEPLETED, TARGET_GRADUATED
        self.trades_count: int = 0
        self.wins_count: int = 0
        self.losses_count: int = 0
        self.gross_profit_cents: int = 0
        self.gross_loss_cents: int = 0
        self.net_pnl_cents: int = 0
        self.milestones_completed: int = 0
        self.last_update_ts: float = time.time()

    @property
    def capital_usd(self) -> float:
        return round(self.capital_cents / 100.0, 2)

    @property
    def net_pnl_usd(self) -> float:
        return round(self.net_pnl_cents / 100.0, 2)

    @property
    def gross_profit_usd(self) -> float:
        return round(self.gross_profit_cents / 100.0, 2)

    @property
    def gross_loss_usd(self) -> float:
        return round(self.gross_loss_cents / 100.0, 2)

    @property
    def win_rate_pct(self) -> float:
        if self.trades_count == 0:
            return 0.0
        return round((self.wins_count / self.trades_count) * 100.0, 1)

    @property
    def progress_pct(self) -> float:
        if self.target_milestone_cents <= 0:
            return 0.0
        pct = (self.capital_cents / self.target_milestone_cents) * 100.0
        return round(min(100.0, max(0.0, pct)), 1)

    def is_active(self) -> bool:
        return self.status != "HALTED_DEPLETED" and self.capital_cents > 0

    def get_initial_budget(self) -> int:
        """Calculates whole-dollar initial order budget: round_half_up(capital / 11)."""
        if not self.is_active():
            return 0
        budget = round_half_up_int(self.capital_cents / 1100.0)
        return max(1, min(budget, 20))

    def get_reversal_budget(self) -> int:
        """Calculates whole-dollar reversal order budget: round_half_up(capital * 10 / 11)."""
        if not self.is_active():
            return 0
        budget = round_half_up_int((self.capital_cents * 10) / 1100.0)
        max_cap = self.max_reversal_stake_cents // 100
        return max(1, min(budget, max_cap))

    def record_win(self, profit_cents: int) -> bool:
        """Records winning trade payout. Returns True if milestone ($100) was crossed."""
        self.trades_count += 1
        self.wins_count += 1
        self.gross_profit_cents += profit_cents
        self.net_pnl_cents += profit_cents
        self.capital_cents += profit_cents
        self.last_update_ts = time.time()
        self.status = "ACTIVE"

        log.info(
            "[%s_WIN] Profit=+%dc | New Capital=$%.2f (Progress: %.1f%% to $100)",
            self.asset.upper(), profit_cents, self.capital_usd, self.progress_pct,
        )

        if self.capital_cents >= self.target_milestone_cents:
            self.status = "TARGET_GRADUATED"
            log.info(
                "[%s_TARGET_GRADUATED] 🎉 Asset reached $100 milestone! Capital=$%.2f",
                self.asset.upper(), self.capital_usd,
            )
            return True
        return False

    def record_initial_loss(self, loss_cents: int) -> None:
        """Records initial trade loss (reversal hedge will follow)."""
        self.trades_count += 1
        self.losses_count += 1
        self.gross_loss_cents += loss_cents
        self.net_pnl_cents -= loss_cents
        self.capital_cents = max(0, self.capital_cents - loss_cents)
        self.last_update_ts = time.time()
        self.status = "REVERSAL_ACTIVE"
        log.info(
            "[%s_INITIAL_LOSS] Loss=-%dc | Remaining Capital=$%.2f",
            self.asset.upper(), loss_cents, self.capital_usd,
        )

    def record_reversal_loss(self, loss_cents: int) -> None:
        """
        Catastrophic reversal hedge loss.
        The cell's capital is exhausted ($0.00) and the bulkhead slams shut.
        Cell is halted until revived by a winning market's harvest grant.
        """
        self.trades_count += 1
        self.losses_count += 1
        self.gross_loss_cents += loss_cents
        self.net_pnl_cents -= loss_cents
        self.capital_cents = 0
        self.status = "HALTED_DEPLETED"
        self.last_update_ts = time.time()
        log.warning(
            "[%s_REVERSAL_LOSS] ⛔ Reversal hedge failed! Cell depleted to $0.00. Status -> HALTED_DEPLETED",
            self.asset.upper(),
        )

    def revive(self, grant_cents: int = 1200) -> None:
        """Revives a halted cell with a grant from a winning market."""
        self.capital_cents = grant_cents
        self.status = "ACTIVE"
        self.last_update_ts = time.time()
        log.info(
            "[%s_REVIVED] 🟢 Cell revived with $%.2f grant from winning peer! Status -> ACTIVE",
            self.asset.upper(), grant_cents / 100.0,
        )

    def reset_after_graduation(self, self_seed_cents: int = 1200) -> None:
        """Resets the cell to baseline capital after profits have been swept."""
        self.capital_cents = self_seed_cents
        self.milestones_completed += 1
        self.status = "ACTIVE"
        self.last_update_ts = time.time()
        log.info(
            "[%s_CYCLE_RESET] Harvest completed. Seed reset to $%.2f. Resuming compounding run #%d.",
            self.asset.upper(), self_seed_cents / 100.0, self.milestones_completed + 1,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset.upper(),
            "capital_usd": self.capital_usd,
            "status": self.status,
            "initial_budget": self.get_initial_budget(),
            "reversal_budget": self.get_reversal_budget(),
            "trades": self.trades_count,
            "wins": self.wins_count,
            "losses": self.losses_count,
            "win_rate": self.win_rate_pct,
            "gross_profit": self.gross_profit_usd,
            "gross_loss": self.gross_loss_usd,
            "net_pnl": self.net_pnl_usd,
            "progress_pct": self.progress_pct,
            "milestones_completed": self.milestones_completed,
            "is_active": self.is_active(),
        }


class MarketCellManager:
    """
    Manages the 6 isolated market cells, computes aggregate fleet metrics,
    and coordinates the autonomous Reviver & Dividend Harvest engine.
    """

    def __init__(
        self,
        assets: Optional[List[str]] = None,
        initial_cell_capital_cents: int = 1200,  # $12 per cell ($72 total)
        milestone_target_cents: int = 10000,    # $100 milestone target
    ):
        self.supported_assets = [a.lower().strip() for a in (assets or ["btc", "eth", "sol", "doge", "xrp", "bnb"])]
        self.initial_cell_capital_cents = initial_cell_capital_cents
        self.milestone_target_cents = milestone_target_cents

        self.cells: Dict[str, MarketCell] = {
            a: MarketCell(
                asset=a,
                initial_capital_cents=initial_cell_capital_cents,
                target_milestone_cents=milestone_target_cents,
            )
            for a in self.supported_assets
        }

        self.harvest_history: List[HarvestEvent] = []
        self.total_swept_usd: float = 0.0

    def get_cell(self, asset: str) -> Optional[MarketCell]:
        return self.cells.get(asset.lower().strip())

    @property
    def total_fleet_equity_usd(self) -> float:
        total_cents = sum(c.capital_cents for c in self.cells.values())
        return round(total_cents / 100.0, 2)

    @property
    def active_cells_count(self) -> int:
        return sum(1 for c in self.cells.values() if c.is_active())

    @property
    def halted_cells(self) -> List[str]:
        return [c.asset for c in self.cells.values() if c.status == "HALTED_DEPLETED"]

    @property
    def halted_cells_count(self) -> int:
        return len(self.halted_cells)

    def check_and_execute_harvest(self, winning_asset: str) -> Optional[HarvestEvent]:
        """
        Executes the Autonomous Reviver & Dividend Harvest:
        1. Winning market keeps $12 self-seed.
        2. Scans for any currently halted markets (H).
        3. Grants $12 to each halted market, reviving them to ACTIVE.
        4. Sweeps the remaining surplus to Binance:
           Sweep = $100 - $12 - (H * $12).
        """
        winner = self.get_cell(winning_asset)
        if not winner or winner.capital_cents < self.milestone_target_cents:
            return None

        now = time.time()
        self_seed_cents = self.initial_cell_capital_cents  # 1200 cents ($12.00)
        halted_assets = list(self.halted_cells)
        halted_count = len(halted_assets)

        revival_grant_per_cell = self.initial_cell_capital_cents  # 1200 cents ($12.00)
        total_revival_funds_cents = halted_count * revival_grant_per_cell

        # Sweep surplus = Target ($100) - SelfSeed ($12) - Revival ($12 * H)
        sweep_cents = max(0, winner.capital_cents - self_seed_cents - total_revival_funds_cents)

        log.info(
            "[HARVEST_TRIGGERED] Winner=%s reached $%.2f! Reviving %d halted cells (%s). Sweeping $%.2f to Binance.",
            winner.asset.upper(), winner.capital_usd, halted_count, halted_assets, sweep_cents / 100.0,
        )

        # 1. Reset winner
        winner.reset_after_graduation(self_seed_cents=self_seed_cents)

        # 2. Revive each halted cell
        for h_asset in halted_assets:
            h_cell = self.get_cell(h_asset)
            if h_cell:
                h_cell.revive(grant_cents=revival_grant_per_cell)

        # 3. Create harvest event
        sweep_usd = round(sweep_cents / 100.0, 2)
        self.total_swept_usd += sweep_usd

        event = HarvestEvent(
            timestamp_ts=now,
            winning_asset=winner.asset.upper(),
            target_capital_usd=round(self.milestone_target_cents / 100.0, 2),
            self_seed_usd=round(self_seed_cents / 100.0, 2),
            halted_revived_assets=[a.upper() for a in halted_assets],
            revival_funds_usd=round(total_revival_funds_cents / 100.0, 2),
            sweep_amount_usd=sweep_usd,
        )
        self.harvest_history.insert(0, event)
        return event

    def get_next_harvest_radar(self) -> Dict[str, Any]:
        """Identifies which market is closest to $100 and computes prospective sweep amount."""
        active_cells = [c for c in self.cells.values() if c.is_active()]
        if not active_cells:
            return {
                "leader_asset": "NONE",
                "leader_equity": 0.0,
                "distance_to_milestone": 0.0,
                "halted_count": self.halted_cells_count,
                "est_sweep_amount_usd": 0.0,
            }

        leader = max(active_cells, key=lambda c: c.capital_cents)
        dist_cents = max(0, self.milestone_target_cents - leader.capital_cents)
        h_count = self.halted_cells_count
        est_sweep_cents = max(0, self.milestone_target_cents - 1200 - (h_count * 1200))

        return {
            "leader_asset": leader.asset.upper(),
            "leader_equity": leader.capital_usd,
            "distance_to_milestone": round(dist_cents / 100.0, 2),
            "halted_count": h_count,
            "halted_assets": [a.upper() for a in self.halted_cells],
            "est_sweep_amount_usd": round(est_sweep_cents / 100.0, 2),
        }

    def get_summary(self) -> Dict[str, Any]:
        return {
            "total_fleet_equity_usd": self.total_fleet_equity_usd,
            "active_cells_count": self.active_cells_count,
            "halted_cells_count": self.halted_cells_count,
            "total_swept_usd": round(self.total_swept_usd, 2),
            "sweeps_count": len(self.harvest_history),
            "next_harvest_radar": self.get_next_harvest_radar(),
            "cells": {a: c.to_dict() for a, c in self.cells.items()},
            "recent_harvests": [h.to_dict() for h in self.harvest_history[:10]],
        }
