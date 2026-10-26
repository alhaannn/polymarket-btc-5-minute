"""
Polymarket Real-Time Data Socket (RTDS) client for Chainlink price feeds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import websockets

log = logging.getLogger("polybot.feeds.rtds")


class RTDSFeed:
    """Consumes Chainlink BTC/USD ticks from Polymarket RTDS WebSocket."""

    def __init__(
        self,
        ws_url: str = "wss://ws-live-data.polymarket.com",
        ping_interval_sec: float = 5.0,
        initial_reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.ws_url = ws_url
        self.ping_interval_sec = ping_interval_sec
        self.initial_reconnect_delay = initial_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.on_tick = on_tick

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

        self.last_tick: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        """Start the background RTDS connection loop."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop connection and cleanup."""
        self._running = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _ping_loop(self, ws: Any) -> None:
        """Keepalive loop sending PING text frames."""
        try:
            while self._running:
                await asyncio.sleep(self.ping_interval_sec)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("RTDS ping error: %s", e)

    async def _run_loop(self) -> None:
        delay = self.initial_reconnect_delay
        while self._running:
            try:
                log.info("Connecting to RTDS WebSocket at %s...", self.ws_url)
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    log.info("Connected to RTDS WebSocket")
                    delay = self.initial_reconnect_delay

                    # Subscribe to crypto_prices_chainlink
                    sub_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "update",
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub_msg))

                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    async for message in ws:
                        if not self._running:
                            break
                        if not message or message == "PONG":
                            continue

                        self._process_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                log.warning("RTDS connection error: %s. Reconnecting in %.1fs...", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.max_reconnect_delay)
            finally:
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()

    def _process_message(self, message: str) -> None:
        try:
            recv_ms = int(time.time() * 1000)
            data = json.loads(message)
            payload = data.get("payload", {})
            symbol = str(payload.get("symbol", "")).lower()

            if symbol == "btc/usd":
                price = float(payload.get("value", 0.0))
                ts_ms = int(payload.get("timestamp", recv_ms))

                tick = {
                    "received_at_ms": recv_ms,
                    "timestamp_ms": ts_ms,
                    "feed": "chainlink",
                    "price": price,
                    "raw": data,
                }
                self.last_tick = tick
                if self.on_tick:
                    self.on_tick(tick)
        except Exception as e:
            log.debug("Error processing RTDS message: %s", e)
