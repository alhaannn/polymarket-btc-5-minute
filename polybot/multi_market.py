"""
Multi-Market Orchestration & Concurrency Engine.
Manages parallel feeds, 6 discrete asset trackers, and the Global Concurrency Arbiter
to guarantee zero margin collisions and seamless multi-asset order execution.
"""
from __future__ import annotations

import collections
import logging
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from polybot.capital.cell import MarketCell, MarketCellManager

log = logging.getLogger("polybot.multi_market")

SUPPORTED_ASSETS = ["btc", "eth", "sol", "doge", "xrp", "bnb"]


class GlobalConcurrencyArbiter:
    """
    Guarantees zero margin collision across all 6 markets by controlling
    simultaneous open taker orders and unhedged positions based on total equity.
    """

    def __init__(self):
        # asset -> {'order_type': str, 'budget_usd': float, 'acquired_at_ts': float}
        self.active_slots: Dict[str, Dict[str, Any]] = {}
        self.queued_assets: Deque[str] = collections.deque(maxlen=10)

    def get_max_slots(self, fleet_equity_usd: float) -> int:
        if fleet_equity_usd < 50.0:
            return 1
        elif fleet_equity_usd < 150.0:
            return 2
        elif fleet_equity_usd < 300.0:
            return 3
        else:
            return 4

    def can_acquire(self, asset: str, fleet_equity_usd: float) -> bool:
        asset_clean = asset.lower().strip()
        if asset_clean in self.active_slots:
            return True  # Already holds a slot (e.g. upgrading initial to reversal)
        max_slots = self.get_max_slots(fleet_equity_usd)
        return len(self.active_slots) < max_slots

    def acquire(self, asset: str, order_type: str, budget_usd: float, fleet_equity_usd: float) -> bool:
        asset_clean = asset.lower().strip()
        asset_upper = asset_clean.upper()
        if not self.can_acquire(asset_clean, fleet_equity_usd):
            if asset_upper not in self.queued_assets:
                self.queued_assets.append(asset_upper)
            log.warning(
                "[ARBITER_REJECT] Slot denied for %s (Max: %d, In Use: %d). Queued.",
                asset_upper, self.get_max_slots(fleet_equity_usd), len(self.active_slots),
            )
            return False

        if asset_upper in self.queued_assets:
            try:
                self.queued_assets.remove(asset_upper)
            except ValueError:
                pass

        self.active_slots[asset_clean] = {
            "order_type": order_type,
            "budget_usd": budget_usd,
            "acquired_at_ts": time.time(),
        }
        log.info(
            "[ARBITER_LOCK] Slot acquired by %s (%s, $%.2f). Active slots: %d/%d.",
            asset_clean.upper(), order_type, budget_usd,
            len(self.active_slots), self.get_max_slots(fleet_equity_usd),
        )
        return True

    def release(self, asset: str) -> None:
        asset_clean = asset.lower().strip()
        if asset_clean in self.active_slots:
            del self.active_slots[asset_clean]
            log.info("[ARBITER_RELEASE] Slot freed by %s. Active slots remaining: %d.",
                     asset_clean.upper(), len(self.active_slots))

    def get_state(self, fleet_equity_usd: float) -> Dict[str, Any]:
        max_s = self.get_max_slots(fleet_equity_usd)
        return {
            "max_slots": max_s,
            "active_count": len(self.active_slots),
            "active_assets": [a.upper() for a in self.active_slots.keys()],
            "active_slots_details": {a.upper(): v for a, v in self.active_slots.items()},
            "queued_assets": [a.upper() for a in self.queued_assets],
            "total_open_exposure_usd": round(sum(s.get("budget_usd", 0.0) for s in self.active_slots.values()), 2),
            "status_text": f"SLOT {len(self.active_slots)}/{max_s} IN USE" if self.active_slots else f"SLOT 0/{max_s} (STANDBY)",
        }


class AssetMarketTracker:
    """Tracks state, ticks, prices, probabilities, and triggers for an individual asset."""

    def __init__(self, asset: str):
        self.asset = asset.lower().strip()
        self.slug: str = f"{self.asset}-updown-5m-waiting"
        self.strike_k: Optional[float] = None
        self.spot_p: Optional[float] = None
        self.up_prob: float = 50.0
        self.down_prob: float = 50.0
        self.up_ask: Optional[float] = None
        self.down_ask: Optional[float] = None
        self.up_bid: Optional[float] = None
        self.down_bid: Optional[float] = None
        self.tokens: Dict[str, str] = {}  # 'Up' -> token_id, 'Down' -> token_id
        self.condition_id: Optional[str] = None
        self.in_trigger_window: bool = False
        self.signal_detected: Optional[str] = None  # 'UP', 'DOWN', or None
        self.last_tick_ts: float = 0.0

    def update_market_info(self, market_dict: Dict[str, Any]) -> None:
        self.slug = market_dict.get("slug", self.slug)
        self.tokens = market_dict.get("tokens", {})
        self.condition_id = market_dict.get("condition_id")

    def update_spot_price(self, price: float, ts_ms: Optional[int] = None) -> None:
        self.spot_p = price
        self.last_tick_ts = time.time()
        if self.strike_k is None and price > 0:
            self.strike_k = price

    def update_probabilities(self, up_prob: float, down_prob: float) -> None:
        self.up_prob = round(up_prob, 1)
        self.down_prob = round(down_prob, 1)

    def update_book(self, up_ask: Optional[float], down_ask: Optional[float],
                    up_bid: Optional[float] = None, down_bid: Optional[float] = None) -> None:
        self.up_ask = up_ask
        self.down_ask = down_ask
        self.up_bid = up_bid
        self.down_bid = down_bid

    @property
    def price_diff(self) -> float:
        if self.spot_p is not None and self.strike_k is not None:
            return round(self.spot_p - self.strike_k, 4)
        return 0.0

    def to_dict(self, cell: Optional[MarketCell] = None) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "asset": self.asset.upper(),
            "slug": self.slug,
            "strike_k": self.strike_k,
            "spot_p": self.spot_p,
            "diff": self.price_diff,
            "up_prob": self.up_prob,
            "down_prob": self.down_prob,
            "up_ask": self.up_ask,
            "down_ask": self.down_ask,
            "up_bid": self.up_bid,
            "down_bid": self.down_bid,
            "in_trigger_window": self.in_trigger_window,
            "signal_detected": self.signal_detected,
            "last_tick_ts": self.last_tick_ts,
        }
        if cell:
            res.update({
                "cell_equity": cell.capital_usd,
                "cell_status": cell.status,
                "initial_budget": cell.get_initial_budget(),
                "reversal_budget": cell.get_reversal_budget(),
                "trades": cell.trades_count,
                "wins": cell.wins_count,
                "losses": cell.losses_count,
                "win_rate": cell.win_rate_pct,
                "gross_profit": cell.gross_profit_usd,
                "gross_loss": cell.gross_loss_usd,
                "net_pnl": cell.net_pnl_usd,
                "progress_pct": cell.progress_pct,
            })
        return res


class MultiMarketOrchestrator:
    """
    Coordinates multi-asset feeds, tracks 6 discrete markets, executes
    independent cell sizing, and manages the Global Concurrency Arbiter.
    """

    def __init__(
        self,
        assets: Optional[List[str]] = None,
        cell_manager: Optional[MarketCellManager] = None,
        arbiter: Optional[GlobalConcurrencyArbiter] = None,
    ):
        self.assets = [a.lower().strip() for a in (assets or SUPPORTED_ASSETS)]
        self.cell_manager = cell_manager or MarketCellManager(assets=self.assets)
        self.arbiter = arbiter or GlobalConcurrencyArbiter()
        self.trackers: Dict[str, AssetMarketTracker] = {a: AssetMarketTracker(a) for a in self.assets}
        self.window_start: int = 0
        self.window_end: int = 0

    def on_tick(self, tick: Dict[str, Any]) -> None:
        """Dispatches an incoming price tick to the corresponding asset tracker."""
        asset = str(tick.get("asset", "")).lower().strip()
        price = float(tick.get("price", 0.0))
        if asset in self.trackers and price > 0:
            self.trackers[asset].update_spot_price(price, tick.get("timestamp_ms"))

    def get_matrix_snapshot(self) -> Dict[str, Any]:
        """Returns the complete 6-market matrix and arbiter snapshot for dashboard consumption."""
        fleet_equity = self.cell_manager.total_fleet_equity_usd
        arbiter_state = self.arbiter.get_state(fleet_equity)
        radar = self.cell_manager.get_next_harvest_radar()

        matrix = {}
        for a in self.assets:
            cell = self.cell_manager.get_cell(a)
            matrix[a] = self.trackers[a].to_dict(cell=cell)

        return {
            "fleet_equity_usd": fleet_equity,
            "active_cells_count": self.cell_manager.active_cells_count,
            "halted_cells_count": self.cell_manager.halted_cells_count,
            "total_swept_usd": self.cell_manager.total_swept_usd,
            "arbiter": arbiter_state,
            "next_harvest_radar": radar,
            "matrix": matrix,
            "recent_harvests": [h.to_dict() for h in self.cell_manager.harvest_history[:5]],
        }
