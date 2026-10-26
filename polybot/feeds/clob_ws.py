"""
Polymarket CLOB WebSocket market feed and local L2 order book maintainer.
Reconstructs books from snapshots and deltas, with REST fallback support.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import websockets

from polybot.gamma import PolymarketClient

log = logging.getLogger("polybot.feeds.clob_ws")


class TokenOrderBook:
    """Maintains sorted L2 order book for a single token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        # price -> size
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_update_ts: float = 0.0

    def apply_snapshot(self, bids: List[Dict[str, Any]], asks: List[Dict[str, Any]]) -> None:
        self.bids.clear()
        self.asks.clear()
        for b in bids:
            price = float(b["price"])
            size = float(b["size"])
            if size > 0:
                self.bids[price] = size
        for a in asks:
            price = float(a["price"])
            size = float(a["size"])
            if size > 0:
                self.asks[price] = size
        self.last_update_ts = time.time()

    def apply_delta(self, price: float, size: float, side: str) -> None:
        book = self.bids if side.upper() == "BUY" else self.asks
        if size <= 1e-9:
            book.pop(price, None)
        else:
            book[price] = size
        self.last_update_ts = time.time()

    def get_sorted_bids(self) -> List[Tuple[float, float]]:
        """Returns bids sorted highest price first: [(price, size), ...]"""
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)

    def get_sorted_asks(self) -> List[Tuple[float, float]]:
        """Returns asks sorted lowest price first: [(price, size), ...]"""
        return sorted(self.asks.items(), key=lambda x: x[0])

    def best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    def spread(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is not None and ba is not None:
            return ba - bb
        return None

    def simulate_buy(self, usd: float) -> Optional[Tuple[float, float, float]]:
        """
        Simulate walking the ask book spending `usd`.
        Returns (avg_fill_price, total_shares, total_spent_usd) or None if depth insufficient.
        """
        asks = self.get_sorted_asks()
        if not asks or usd <= 0:
            return None

        remaining = usd
        shares = 0.0
        spent = 0.0

        for price, size in asks:
            level_usd = price * size
            take_usd = min(remaining, level_usd)
            shares += take_usd / price
            spent += take_usd
            remaining -= take_usd
            if remaining <= 1e-9:
                break

        if remaining > 1e-6 or shares <= 0:
            return None  # book too thin

        avg_price = spent / shares
        return avg_price, shares, spent

    def simulate_sell(self, shares_to_sell: float) -> Optional[Tuple[float, float, float]]:
        """
        Simulate selling `shares_to_sell` to the bids.
        Returns (avg_price, shares_sold, usd_received) or None if depth insufficient.
        """
        bids = self.get_sorted_bids()
        if not bids or shares_to_sell <= 0:
            return None

        remaining_shares = shares_to_sell
        usd_received = 0.0
        shares_sold = 0.0

        for price, size in bids:
            take_shares = min(remaining_shares, size)
            usd_received += take_shares * price
            shares_sold += take_shares
            remaining_shares -= take_shares
            if remaining_shares <= 1e-9:
                break

        if remaining_shares > 1e-6 or shares_sold <= 0:
            return None

        avg_price = usd_received / shares_sold
        return avg_price, shares_sold, usd_received


class CLOBOrderBookFeed:
    """Maintains local order books for Up/Down tokens via WebSocket with REST fallback."""

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        rest_client: Optional[PolymarketClient] = None,
        on_raw_message: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.ws_url = ws_url
        self.rest_client = rest_client or PolymarketClient()
        self.on_raw_message = on_raw_message

        self.books: Dict[str, TokenOrderBook] = {}
        self.token_to_side: Dict[str, str] = {}  # token_id -> 'Up' or 'Down'

        self._subscribed_tokens: List[str] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._ws_connected = False

    def track_tokens(self, tokens_map: Dict[str, str]) -> None:
        """Set tokens to track: {'Up': token_up, 'Down': token_down}"""
        self.token_to_side.clear()
        self._subscribed_tokens = list(tokens_map.values())
        for side, tid in tokens_map.items():
            self.token_to_side[tid] = side
            if tid not in self.books:
                self.books[tid] = TokenOrderBook(tid)

    def get_book(self, side_or_token: str) -> Optional[TokenOrderBook]:
        if side_or_token in self.books:
            return self.books[side_or_token]
        # Check if passed as "Up" or "Down"
        for tid, side in self.token_to_side.items():
            if side.lower() == side_or_token.lower():
                return self.books[tid]
        return None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        delay = 1.0
        while self._running:
            if not self._subscribed_tokens:
                await asyncio.sleep(0.5)
                continue

            try:
                log.info("Connecting to CLOB WebSocket at %s...", self.ws_url)
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    log.info("Connected to CLOB WebSocket")
                    delay = 1.0

                    sub_msg = {
                        "assets_ids": self._subscribed_tokens,
                        "type": "market",
                    }
                    await ws.send(json.dumps(sub_msg))

                    async for message in ws:
                        if not self._running:
                            break
                        self._process_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._ws_connected = False
                if not self._running:
                    break
                log.warning("CLOB WS error: %s. Reconnecting in %.1fs...", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)

    def _process_message(self, message: str) -> None:
        try:
            recv_ms = int(time.time() * 1000)
            data = json.loads(message)

            if self.on_raw_message:
                self.on_raw_message({"received_at_ms": recv_ms, "raw": data})

            # Handle initial snapshot (list of book objects)
            if isinstance(data, list):
                for item in data:
                    asset_id = item.get("asset_id")
                    if asset_id and asset_id in self.books:
                        bids = item.get("bids", [])
                        asks = item.get("asks", [])
                        self.books[asset_id].apply_snapshot(bids, asks)
                return

            # Handle delta price changes
            if isinstance(data, dict):
                price_changes = data.get("price_changes", [])
                for chg in price_changes:
                    asset_id = chg.get("asset_id")
                    if asset_id and asset_id in self.books:
                        price = float(chg.get("price", 0.0))
                        size = float(chg.get("size", 0.0))
                        side = chg.get("side", "")
                        self.books[asset_id].apply_delta(price, size, side)

        except Exception as e:
            log.debug("Error processing CLOB WS message: %s", e)

    def poll_rest_fallback(self) -> None:
        """Poll order book via REST if WebSocket is disconnected or stale."""
        for tid in self._subscribed_tokens:
            if tid in self.books:
                book = self.rest_client.get_orderbook(tid)
                bids_raw = [{"price": p, "size": s} for p, s in book["bids"]]
                asks_raw = [{"price": p, "size": s} for p, s in book["asks"]]
                self.books[tid].apply_snapshot(bids_raw, asks_raw)
