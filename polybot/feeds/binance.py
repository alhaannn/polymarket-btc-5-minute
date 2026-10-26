"""
Binance WebSocket feed client for real-time BTC/USDT trade prices.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import websockets

log = logging.getLogger("polybot.feeds.binance")


class BinanceFeed:
    """Consumes real-time BTC/USDT trades from Binance public WebSocket."""

    def __init__(
        self,
        ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade",
        initial_reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.ws_url = ws_url
        self.initial_reconnect_delay = initial_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.on_tick = on_tick

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.last_tick: Optional[Dict[str, Any]] = None

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
                log.warning("Binance connection error: %s. Reconnecting in %.1fs...", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.max_reconnect_delay)

    def _process_message(self, message: str) -> None:
        try:
            recv_ms = int(time.time() * 1000)
            data = json.loads(message)
            price = float(data.get("p", 0.0))
            event_ts = int(data.get("T", recv_ms))

            tick = {
                "received_at_ms": recv_ms,
                "timestamp_ms": event_ts,
                "feed": "binance",
                "price": price,
                "raw": data,
            }
            self.last_tick = tick
            if self.on_tick:
                self.on_tick(tick)
        except Exception as e:
            log.debug("Error processing Binance message: %s", e)
