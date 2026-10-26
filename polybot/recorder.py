"""
24/7 Lossless Data Recorder for Polymarket BTC 5-minute Up/Down markets.
Records raw Chainlink ticks, Binance trades, and CLOB order book updates in JSONL.
Rotates and compresses past windows into .tar.gz archives with manifest tracking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from polybot.config import PolybotConfig, RecorderConfig
from polybot.feeds.binance import BinanceFeed
from polybot.feeds.clob_ws import CLOBOrderBookFeed
from polybot.feeds.health import FeedHealthTracker
from polybot.feeds.rtds import RTDSFeed
from polybot.gamma import PolymarketClient

log = logging.getLogger("polybot.recorder")


import queue
import threading


class WindowWriter:
    """High-performance non-blocking batch writer for a 5-minute window."""

    def __init__(self, window_dir: Path):
        self.window_dir = window_dir
        self.window_dir.mkdir(parents=True, exist_ok=True)

        self.chainlink_path = self.window_dir / "chainlink.jsonl"
        self.binance_path = self.window_dir / "binance.jsonl"
        self.market_path = self.window_dir / "market.jsonl"
        self.metadata_path = self.window_dir / "metadata.json"
        self.resolution_path = self.window_dir / "resolution.json"

        self._queue: queue.Queue = queue.Queue(maxsize=200000)
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="WindowWriterWorker")
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        with open(self.chainlink_path, "a", encoding="utf-8") as f_cl, \
             open(self.binance_path, "a", encoding="utf-8") as f_bn, \
             open(self.market_path, "a", encoding="utf-8") as f_mkt:

            files = {
                "chainlink": f_cl,
                "binance": f_bn,
                "market": f_mkt,
            }

            last_flush = time.time()
            batch_count = 0

            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    item = None

                if item is not None:
                    feed_type, line = item
                    target_file = files.get(feed_type)
                    if target_file:
                        target_file.write(line if line.endswith("\n") else line + "\n")
                        batch_count += 1
                    self._queue.task_done()

                now = time.time()
                # Batch flush every 0.5 seconds or every 250 items
                if batch_count >= 250 or (now - last_flush >= 0.5 and batch_count > 0):
                    for f in files.values():
                        f.flush()
                    batch_count = 0
                    last_flush = now

            # Ensure final flush on shutdown
            for f in files.values():
                f.flush()

    def write_chainlink(self, tick: Any) -> None:
        line = tick if isinstance(tick, str) else json.dumps(tick)
        try:
            self._queue.put_nowait(("chainlink", line))
        except queue.Full:
            log.warning("Writer queue full for chainlink tick")

    def write_binance(self, tick: Any) -> None:
        line = tick if isinstance(tick, str) else json.dumps(tick)
        try:
            self._queue.put_nowait(("binance", line))
        except queue.Full:
            log.warning("Writer queue full for binance tick")

    def write_market(self, event: Any) -> None:
        line = event if isinstance(event, str) else json.dumps(event)
        try:
            self._queue.put_nowait(("market", line))
        except queue.Full:
            log.warning("Writer queue full for market event")

    def write_metadata(self, metadata: Dict[str, Any]) -> None:
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

    def write_resolution(self, resolution: Dict[str, Any]) -> None:
        with open(self.resolution_path, "w", encoding="utf-8") as f:
            json.dump(resolution, f, indent=2)

    def close(self) -> None:
        self._stop_event.set()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)



class MarketRecorder:
    """24/7 background recorder orchestrating feeds and multi-window rotation."""

    def __init__(self, config: Optional[PolybotConfig] = None):
        self.config = config or PolybotConfig()
        self.client = PolymarketClient(
            gamma_url=self.config.feeds.gamma_rest_url,
            clob_url=self.config.feeds.clob_rest_url,
        )
        self.health = FeedHealthTracker(
            max_staleness_sec=self.config.feeds.max_feed_staleness_sec,
            max_divergence_pct=self.config.feeds.max_divergence_pct,
        )

        self.rtds_feed = RTDSFeed(
            ws_url=self.config.feeds.rtds_ws_url,
            ping_interval_sec=self.config.feeds.rtds_ping_interval_sec,
            on_tick=self._on_chainlink_tick,
        )
        self.binance_feed = BinanceFeed(
            ws_url=self.config.feeds.binance_ws_url,
            on_tick=self._on_binance_tick,
        )
        self.clob_feed = CLOBOrderBookFeed(
            ws_url=self.config.feeds.clob_ws_url,
            rest_client=self.client,
            on_raw_message=self._on_clob_message,
        )

        self.current_window_start: Optional[int] = None
        self.current_writer: Optional[WindowWriter] = None
        self.current_metadata: Optional[Dict[str, Any]] = None
        self.strike_price_K: Optional[float] = None

        self._pending_resolutions: List[Dict[str, Any]] = []
        self._running = False
        self._orchestrator_task: Optional[asyncio.Task] = None

    def _on_chainlink_tick(self, tick: Dict[str, Any]) -> None:
        price = tick["price"]
        ts_ms = tick["timestamp_ms"]
        gap = self.health.record_tick("chainlink", price, ts_ms)
        if gap and self.current_writer:
            self.current_writer.write_chainlink({
                "received_at_ms": tick["received_at_ms"],
                "event_type": "gap",
                "gap_duration_sec": gap.gap_duration_sec,
                "start_ts_ms": gap.start_ts_ms,
                "end_ts_ms": gap.end_ts_ms,
            })

        if self.strike_price_K is None and price > 0:
            self.strike_price_K = price
            if self.current_metadata and self.current_writer:
                self.current_metadata["K"] = price
                self.current_writer.write_metadata(self.current_metadata)

        if self.current_writer:
            self.current_writer.write_chainlink(tick)

    def _on_binance_tick(self, tick: Dict[str, Any]) -> None:
        price = tick["price"]
        ts_ms = tick["timestamp_ms"]
        gap = self.health.record_tick("binance", price, ts_ms)
        if gap and self.current_writer:
            self.current_writer.write_binance({
                "received_at_ms": tick["received_at_ms"],
                "event_type": "gap",
                "gap_duration_sec": gap.gap_duration_sec,
                "start_ts_ms": gap.start_ts_ms,
                "end_ts_ms": gap.end_ts_ms,
            })

        if self.current_writer:
            self.current_writer.write_binance(tick)

    def _on_clob_message(self, event: Dict[str, Any]) -> None:
        if self.current_writer:
            self.current_writer.write_market(event)

    async def start(self) -> None:
        self._running = True
        self.config.recorder.raw_dir.mkdir(parents=True, exist_ok=True)
        log.info("Starting feeds...")
        await self.rtds_feed.start()
        await self.binance_feed.start()
        await self.clob_feed.start()
        self._orchestrator_task = asyncio.create_task(self._run_orchestrator())

    async def stop(self) -> None:
        log.info("Stopping recorder...")
        self._running = False
        if self._orchestrator_task and not self._orchestrator_task.done():
            self._orchestrator_task.cancel()
        await self.rtds_feed.stop()
        await self.binance_feed.stop()
        await self.clob_feed.stop()
        if self.current_writer:
            self.current_writer.close()

    async def _run_orchestrator(self) -> None:
        while self._running:
            try:
                now = int(time.time())
                window_start = (now // 300) * 300

                if self.current_window_start != window_start:
                    await self._switch_window(window_start)

                await self._check_pending_resolutions()
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Recorder orchestrator error: %s", e, exc_info=True)
                await asyncio.sleep(2.0)

    async def _switch_window(self, new_window_start: int) -> None:
        log.info("Transitioning to window %d (current=%s)...", new_window_start, self.current_window_start)
        # Close current writer
        if self.current_writer and self.current_window_start:
            self.current_writer.close()
            # Queue for resolution polling
            if self.current_metadata:
                self._pending_resolutions.append({
                    "slug": self.current_metadata["slug"],
                    "window_start": self.current_window_start,
                    "window_dir": str(self.config.recorder.raw_dir / str(self.current_window_start)),
                    "check_count": 0,
                })

        self.current_window_start = new_window_start
        self.strike_price_K = self.health.chainlink.last_price

        window_dir = self.config.recorder.raw_dir / str(new_window_start)
        self.current_writer = WindowWriter(window_dir)

        # Discover market metadata
        slug = f"btc-updown-5m-{new_window_start}"
        market_info = None
        for _ in range(5):
            market_info = self.client.get_market(new_window_start)
            if market_info:
                break
            await asyncio.sleep(1.5)

        tokens = market_info.get("tokens", {}) if market_info else {}
        self.current_metadata = {
            "slug": slug,
            "window_start": new_window_start,
            "window_end": new_window_start + 300,
            "tokens": tokens,
            "K": self.strike_price_K,
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "discovered": bool(market_info),
        }
        self.current_writer.write_metadata(self.current_metadata)

        if tokens:
            self.clob_feed.track_tokens(tokens)
            log.info("Tracking market %s with tokens: %s", slug, tokens)
        else:
            log.warning("Market tokens not found yet for %s", slug)

    async def _check_pending_resolutions(self) -> None:
        """Poll Gamma for settlement outcomePrices of recently closed windows."""
        for item in list(self._pending_resolutions):
            slug = item["slug"]
            item["check_count"] += 1
            winner = self.client.get_winner(slug)
            if winner:
                log.info("Resolved %s -> Winner: %s", slug, winner)
                res_path = Path(item["window_dir"]) / "resolution.json"
                with open(res_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "slug": slug,
                        "winner": winner,
                        "resolved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }, f, indent=2)
                self._pending_resolutions.remove(item)
                # Rotate and compress
                self._rotate_and_compress()
            elif item["check_count"] > 60:  # ~10-15 minutes
                log.warning("Window %s resolution timed out", slug)
                self._pending_resolutions.remove(item)

    def _rotate_and_compress(self) -> None:
        """Compress older completed window folders into .tar.gz and update manifest."""
        raw_dir = self.config.recorder.raw_dir
        manifest_path = self.config.recorder.manifest_file

        manifest: Dict[str, Any] = {"windows": {}}
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                pass

        # Find window dirs that are completed
        subdirs = [d for d in raw_dir.iterdir() if d.is_dir() and d.name.isdigit()]
        subdirs.sort(key=lambda x: int(x.name))

        # Keep at least max_uncompressed_windows uncompressed
        to_compress = subdirs[:-self.config.recorder.max_uncompressed_windows]
        for sdir in to_compress:
            w_start = sdir.name
            tar_path = raw_dir / f"{w_start}.tar.gz"
            if not tar_path.exists():
                log.info("Compressing window %s into %s...", w_start, tar_path.name)
                with tarfile.open(tar_path, "w:gz") as tar:
                    tar.add(sdir, arcname=sdir.name)

                # Read resolution if available
                res_file = sdir / "resolution.json"
                winner = None
                if res_file.exists():
                    try:
                        with open(res_file, "r", encoding="utf-8") as rf:
                            winner = json.load(rf).get("winner")
                    except Exception:
                        pass

                manifest["windows"][w_start] = {
                    "archive": tar_path.name,
                    "compressed_at": time.time(),
                    "winner": winner,
                }
                # Remove uncompressed directory
                shutil.rmtree(sdir, ignore_errors=True)

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
