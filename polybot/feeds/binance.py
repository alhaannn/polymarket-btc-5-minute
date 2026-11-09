"""
Binance WebSocket feed client for real-time BTC/USDT trade prices.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import websockets

log = logging.getLogger("polybot.feeds.binance")


class BinanceFeed:
    """Consumes real-time trade prices from Binance public WebSocket (single or multiplex combined streams)."""

    def __init__(
        self,
        ws_url: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        initial_reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.symbols = [s.lower().strip() for s in symbols] if symbols else ["btcusdt"]
        if ws_url:
            self.ws_url = ws_url
        elif len(self.symbols) > 1:
            streams_param = "/".join(f"{s}@trade" for s in self.symbols)
            self.ws_url = f"wss://stream.binance.com:9443/stream?streams={streams_param}"
        else:
            self.ws_url = f"wss://stream.binance.com:9443/ws/{self.symbols[0]}@trade"

        self.initial_reconnect_delay = initial_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.on_tick = on_tick

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.last_tick: Optional[Dict[str, Any]] = None
        self.last_ticks: Dict[str, Dict[str, Any]] = {}

    def get_last_price(self, asset: str) -> Optional[float]:
        """Returns the most recent trade price for a specific asset (e.g. 'btc', 'eth')."""
        tick = self.last_ticks.get(asset.lower().strip())
        if tick and "price" in tick:
            return float(tick["price"])
        return None

    def get_last_tick(self, asset: str) -> Optional[Dict[str, Any]]:
        """Returns the most recent tick object for a specific asset."""
        return self.last_ticks.get(asset.lower().strip())

    async def start(self) -> None:
        """Start background connection loop."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop connection and cleanup."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        delay = self.initial_reconnect_delay
        while self._running:
            try:
                log.info("Connecting to Binance WebSocket at %s...", self.ws_url)
                async with websockets.connect(self.ws_url) as ws:
                    log.info("Connected to Binance WebSocket")
                    delay = self.initial_reconnect_delay

                    async for message in ws:
                        if not self._running:
                            break
                        self._process_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                if "451" in str(e):
                    log.info("Binance feed geographically restricted (HTTP 451). Price powered by Polymarket RTDS.")
                    await asyncio.sleep(60.0)
                else:
                    log.warning("Binance connection error: %s. Reconnecting in %.1fs...", e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, self.max_reconnect_delay)

    def _process_message(self, message: str) -> None:
        try:
            recv_ms = int(time.time() * 1000)
            data = json.loads(message)

            # Support both combined stream: {"stream": "...", "data": {...}}
            # and direct single stream: {"e": "trade", "p": "...", ...}
            if "stream" in data and isinstance(data.get("data"), dict):
                trade_data = data["data"]
            else:
                trade_data = data

            price = float(trade_data.get("p", 0.0))
            symbol = str(trade_data.get("s", "BTCUSDT")).upper()
            asset = symbol.replace("USDT", "").lower()
            event_ts = int(trade_data.get("T", recv_ms))

            tick = {
                "received_at_ms": recv_ms,
                "timestamp_ms": event_ts,
                "feed": "binance",
                "asset": asset,
                "symbol": symbol,
                "price": price,
                "raw": data,
            }
            self.last_tick = tick
            self.last_ticks[asset] = tick
            if self.on_tick:
                self.on_tick(tick)
        except Exception as e:
            log.debug("Error processing Binance message: %s", e)
