"""
Historical data loader for backtesting.
Reads and merges raw ticks from both polybot JSONL archives and
open-source polyticker directories into a chronological event stream.
"""
from __future__ import annotations

import json
import logging
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

log = logging.getLogger("polybot.backtest.loader")


@dataclass
class BacktestEvent:
    timestamp_sec: float
    event_type: str  # "chainlink", "binance", "market"
    payload: Dict[str, Any]


@dataclass
class WindowData:
    slug: str
    window_start: int
    window_end: int
    tokens: Dict[str, str]  # {"Up": ..., "Down": ...}
    strike_price_K: float
    winner: Optional[str]
    events: List[BacktestEvent] = field(default_factory=list)


class WindowDataLoader:
    """Discovers and parses recorded window folders and .tar.gz archives."""

    def __init__(self, raw_data_dir: Path | str = Path("data/raw")):
        self.raw_data_dir = Path(raw_data_dir)

    def discover_windows(self) -> List[Path]:
        """Finds all window directories and .tar.gz archives, sorted chronologically."""
        if not self.raw_data_dir.exists():
            return []

        entries: List[Tuple[int, Path]] = []
        for p in self.raw_data_dir.iterdir():
            name = p.name
            if p.is_dir() and name.isdigit():
                entries.append((int(name), p))
            elif p.is_file() and name.endswith(".tar.gz"):
                stem = name[:-7]
                if stem.isdigit():
                    entries.append((int(stem), p))

        entries.sort(key=lambda x: x[0])
        return [p for _, p in entries]

    def load_window(self, path: Path) -> Optional[WindowData]:
        """Loads a single window from an uncompressed directory or .tar.gz archive."""
        if path.is_dir():
            return self._load_from_directory(path)
        elif path.is_file() and path.name.endswith(".tar.gz"):
            return self._load_from_tar(path)
        return None

    def _load_from_directory(self, dir_path: Path) -> Optional[WindowData]:
        meta_file = dir_path / "metadata.json"
        res_file = dir_path / "resolution.json"

        metadata: Dict[str, Any] = {}
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception:
                pass

        resolution: Dict[str, Any] = {}
        if res_file.exists():
            try:
                with open(res_file, "r", encoding="utf-8") as f:
                    resolution = json.load(f)
            except Exception:
                pass

        window_start = int(metadata.get("window_start") or dir_path.name)
        window_end = int(metadata.get("window_end") or window_start + 300)
        slug = metadata.get("slug") or f"btc-updown-5m-{window_start}"
        tokens = metadata.get("tokens") or {}
        winner = resolution.get("winner")

        events: List[BacktestEvent] = []
        first_cl_price: Optional[float] = None

        # Load Chainlink ticks
        cl_file = dir_path / "chainlink.jsonl"
        if cl_file.exists():
            with open(cl_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("event_type") == "gap":
                            continue
                        price = float(data.get("price") or data.get("value") or 0.0)
                        if price > 0 and first_cl_price is None:
                            first_cl_price = price
                        ts_ms = int(data.get("timestamp_ms") or data.get("received_at_ms") or data.get("timestamp") or 0)
                        events.append(BacktestEvent(
                            timestamp_sec=ts_ms / 1000.0,
                            event_type="chainlink",
                            payload={"price": price, "raw": data},
                        ))
                    except Exception:
                        continue

        # Load Binance ticks
        bn_file = dir_path / "binance.jsonl"
        if bn_file.exists():
            with open(bn_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        price = float(data.get("price") or data.get("p") or 0.0)
                        ts_ms = int(data.get("timestamp_ms") or data.get("received_at_ms") or data.get("T") or 0)
                        events.append(BacktestEvent(
                            timestamp_sec=ts_ms / 1000.0,
                            event_type="binance",
                            payload={"price": price, "raw": data},
                        ))
                    except Exception:
                        continue

        # Load Market orderbook events
        mkt_file = dir_path / "market.jsonl"
        if mkt_file.exists():
            with open(mkt_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        raw = data.get("raw", data)
                        ts_val = None
                        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                            ts_val = raw[0].get("timestamp")
                        elif isinstance(raw, dict):
                            ts_val = raw.get("timestamp")
                        ts_ms = int(data.get("received_at_ms") or ts_val or (window_start * 1000))
                        events.append(BacktestEvent(
                            timestamp_sec=ts_ms / 1000.0,
                            event_type="market",
                            payload={"raw": raw},
                        ))
                    except Exception:
                        continue

        events.sort(key=lambda e: e.timestamp_sec)
        k_val = float(metadata.get("K") or first_cl_price or 0.0)

        return WindowData(
            slug=slug,
            window_start=window_start,
            window_end=window_end,
            tokens=tokens,
            strike_price_K=k_val,
            winner=winner,
            events=events,
        )

    def _load_from_tar(self, tar_path: Path) -> Optional[WindowData]:
        """Extracts and parses archive directly in memory."""
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                # Find metadata and resolution
                meta_data, res_data = {}, {}
                for member in tar.getmembers():
                    if member.name.endswith("metadata.json"):
                        f = tar.extractfile(member)
                        if f:
                            meta_data = json.load(f)
                    elif member.name.endswith("resolution.json"):
                        f = tar.extractfile(member)
                        if f:
                            res_data = json.load(f)

                stem = tar_path.name[:-7]
                window_start = int(meta_data.get("window_start") or stem)
                window_end = int(meta_data.get("window_end") or window_start + 300)
                slug = meta_data.get("slug") or f"btc-updown-5m-{window_start}"
                tokens = meta_data.get("tokens") or {}
                winner = res_data.get("winner")

                events: List[BacktestEvent] = []
                first_cl_price: Optional[float] = None

                for member in tar.getmembers():
                    if member.name.endswith("chainlink.jsonl"):
                        f = tar.extractfile(member)
                        if f:
                            for line in f:
                                line_str = line.decode("utf-8").strip()
                                if not line_str:
                                    continue
                                d = json.loads(line_str)
                                price = float(d.get("price") or d.get("value") or 0.0)
                                if price > 0 and first_cl_price is None:
                                    first_cl_price = price
                                ts = int(d.get("timestamp_ms") or d.get("received_at_ms") or 0)
                                events.append(BacktestEvent(ts / 1000.0, "chainlink", {"price": price, "raw": d}))
                    elif member.name.endswith("binance.jsonl"):
                        f = tar.extractfile(member)
                        if f:
                            for line in f:
                                line_str = line.decode("utf-8").strip()
                                if not line_str:
                                    continue
                                d = json.loads(line_str)
                                price = float(d.get("price") or d.get("p") or 0.0)
                                ts = int(d.get("timestamp_ms") or d.get("received_at_ms") or d.get("T") or 0)
                                events.append(BacktestEvent(ts / 1000.0, "binance", {"price": price, "raw": d}))
                    elif member.name.endswith("market.jsonl"):
                        f = tar.extractfile(member)
                        if f:
                            for line in f:
                                line_str = line.decode("utf-8").strip()
                                if not line_str:
                                    continue
                                d = json.loads(line_str)
                                raw = d.get("raw", d)
                                ts_val = None
                                if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                                    ts_val = raw[0].get("timestamp")
                                elif isinstance(raw, dict):
                                    ts_val = raw.get("timestamp")
                                ts = int(d.get("received_at_ms") or ts_val or (window_start * 1000))
                                events.append(BacktestEvent(ts / 1000.0, "market", {"raw": raw}))

                events.sort(key=lambda e: e.timestamp_sec)
                k_val = float(meta_data.get("K") or first_cl_price or 0.0)

                return WindowData(
                    slug=slug,
                    window_start=window_start,
                    window_end=window_end,
                    tokens=tokens,
                    strike_price_K=k_val,
                    winner=winner,
                    events=events,
                )
        except Exception as e:
            log.warning("Failed to load archive %s: %s", tar_path.name, e)
            return None
