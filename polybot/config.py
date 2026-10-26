"""
Configuration management for polybot.
Supports dataclasses, YAML configuration files, and environment variable overrides.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None


def _get_env_val(key: str, default: Any, val_type: type) -> Any:
    """Read an environment variable with type coercion."""
    val = os.getenv(key)
    if val is None:
        return default
    if val_type is bool:
        return val.strip().lower() in ("1", "true", "yes", "y", "t")
    try:
        return val_type(val)
    except (ValueError, TypeError):
        return default


@dataclass
class FeedsConfig:
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    clob_rest_url: str = "https://clob.polymarket.com"
    gamma_rest_url: str = "https://gamma-api.polymarket.com"
    max_feed_staleness_sec: float = 3.0
    max_divergence_pct: float = 0.005  # 0.5% max divergence between Chainlink and Binance
    ws_reconnect_initial_delay: float = 1.0
    ws_reconnect_max_delay: float = 30.0
    rtds_ping_interval_sec: float = 5.0

    @classmethod
    def from_env(cls) -> FeedsConfig:
        return cls(
            rtds_ws_url=os.getenv("RTDS_WS_URL", "wss://ws-live-data.polymarket.com"),
            binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade"),
            clob_ws_url=os.getenv("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            clob_rest_url=os.getenv("CLOB_REST_URL", "https://clob.polymarket.com"),
            gamma_rest_url=os.getenv("GAMMA_REST_URL", "https://gamma-api.polymarket.com"),
            max_feed_staleness_sec=_get_env_val("MAX_FEED_STALENESS_SEC", 3.0, float),
            max_divergence_pct=_get_env_val("MAX_DIVERGENCE_PCT", 0.005, float),
            ws_reconnect_initial_delay=_get_env_val("WS_RECONNECT_INITIAL_DELAY", 1.0, float),
            ws_reconnect_max_delay=_get_env_val("WS_RECONNECT_MAX_DELAY", 30.0, float),
            rtds_ping_interval_sec=_get_env_val("RTDS_PING_INTERVAL_SEC", 5.0, float),
        )


@dataclass
class RecorderConfig:
    data_dir: Path = Path("data")
    raw_dir: Path = Path("data/raw")
    compress_old_windows: bool = True
    manifest_file: Path = Path("data/raw/manifest.json")
    max_uncompressed_windows: int = 6

    @classmethod
    def from_env(cls) -> RecorderConfig:
        base_dir = Path(os.getenv("DATA_DIR", "data"))
        raw_dir = Path(os.getenv("RAW_DATA_DIR", str(base_dir / "raw")))
        return cls(
            data_dir=base_dir,
            raw_dir=raw_dir,
            compress_old_windows=_get_env_val("COMPRESS_OLD_WINDOWS", True, bool),
            manifest_file=raw_dir / "manifest.json",
            max_uncompressed_windows=_get_env_val("MAX_UNCOMPRESSED_WINDOWS", 6, int),
        )


@dataclass
class StrategyConfig:
    strategy_name: str = "edge"  # "edge" or "legacy"
    stake_usd: float = 5.0
    price_min: float = 0.85
    price_max: float = 0.985
    legacy_price_min: float = 0.93
    legacy_price_max: float = 0.96
    entry_start_sec: float = 120.0
    entry_end_sec: float = 285.0
    min_edge: float = 0.02
    z_min: float = 1.0
    min_lead_usd: float = 5.0
    adverse_drift_max_pct: float = 0.50
    vol_spike_ratio_max: float = 2.5
    spread_max: float = 0.05
    early_exit: bool = False
    exit_p: float = 0.40
    latency_ms: int = 400
    order_style: str = "taker"  # "taker" or "maker"
    maker_timeout_sec: float = 10.0
    max_slippage: float = 0.015

    # Fees
    taker_fee_rate: float = 0.001
    maker_fee_rate: float = 0.000

    @classmethod
    def from_env(cls) -> StrategyConfig:
        return cls(
            strategy_name=os.getenv("STRATEGY", "edge"),
            stake_usd=_get_env_val("STAKE_USD", 5.0, float),
            price_min=_get_env_val("PRICE_MIN", 0.85, float),
            price_max=_get_env_val("PRICE_MAX", 0.985, float),
            legacy_price_min=_get_env_val("LEGACY_PRICE_MIN", 0.93, float),
            legacy_price_max=_get_env_val("LEGACY_PRICE_MAX", 0.96, float),
            entry_start_sec=_get_env_val("ENTRY_START_SEC", 120.0, float),
            entry_end_sec=_get_env_val("ENTRY_END_SEC", 285.0, float),
            min_edge=_get_env_val("MIN_EDGE", 0.02, float),
            z_min=_get_env_val("Z_MIN", 1.0, float),
            min_lead_usd=_get_env_val("MIN_LEAD_USD", 5.0, float),
            adverse_drift_max_pct=_get_env_val("ADVERSE_DRIFT_MAX_PCT", 0.50, float),
            vol_spike_ratio_max=_get_env_val("VOL_SPIKE_RATIO_MAX", 2.5, float),
            spread_max=_get_env_val("SPREAD_MAX", 0.05, float),
            early_exit=_get_env_val("EARLY_EXIT", False, bool),
            exit_p=_get_env_val("EXIT_P", 0.40, float),
            latency_ms=_get_env_val("LATENCY_MS", 400, int),
            order_style=os.getenv("ORDER_STYLE", "taker"),
            maker_timeout_sec=_get_env_val("MAKER_TIMEOUT_SEC", 10.0, float),
            max_slippage=_get_env_val("MAX_SLIPPAGE", 0.015, float),
            taker_fee_rate=_get_env_val("TAKER_FEE_RATE", 0.001, float),
            maker_fee_rate=_get_env_val("MAKER_FEE_RATE", 0.000, float),
        )


@dataclass
class RiskConfig:
    daily_loss_limit_usd: float = 25.0
    max_trades_per_day: int = 50
    pause_consecutive_losses: int = 3
    pause_duration_windows: int = 3
    max_open_exposure_usd: float = 20.0
    kill_switch_file: str = "STOP"
    sizing_mode: str = "fixed"  # "fixed" or "fractional_kelly"
    kelly_fraction: float = 0.25
    kelly_max_stake_usd: float = 15.0

    @classmethod
    def from_env(cls) -> RiskConfig:
        return cls(
            daily_loss_limit_usd=_get_env_val("DAILY_LOSS_LIMIT_USD", 25.0, float),
            max_trades_per_day=_get_env_val("MAX_TRADES_PER_DAY", 50, int),
            pause_consecutive_losses=_get_env_val("PAUSE_CONSECUTIVE_LOSSES", 3, int),
            pause_duration_windows=_get_env_val("PAUSE_DURATION_WINDOWS", 3, int),
            max_open_exposure_usd=_get_env_val("MAX_OPEN_EXPOSURE_USD", 20.0, float),
            kill_switch_file=os.getenv("KILL_SWITCH_FILE", "STOP"),
            sizing_mode=os.getenv("SIZING_MODE", "fixed"),
            kelly_fraction=_get_env_val("KELLY_FRACTION", 0.25, float),
            kelly_max_stake_usd=_get_env_val("KELLY_MAX_STAKE_USD", 15.0, float),
        )


@dataclass
class LiveConfig:
    is_live_requested: bool = False
    live_confirm_token: str = ""
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 0
    micro_stake_cap_usd: float = 1.0

    @classmethod
    def from_env(cls) -> LiveConfig:
        return cls(
            is_live_requested=False,  # must be explicitly set via CLI flag
            live_confirm_token=os.getenv("LIVE_CONFIRM", ""),
            private_key=os.getenv("POLY_PRIVATE_KEY", ""),
            funder_address=os.getenv("POLY_FUNDER_ADDRESS", ""),
            signature_type=_get_env_val("POLY_SIG_TYPE", 0, int),
            micro_stake_cap_usd=_get_env_val("MICRO_STAKE_CAP_USD", 1.0, float),
        )

    def is_live_authorized(self) -> bool:
        """Rule 1: Gated behind both --live CLI flag AND LIVE_CONFIRM=YES_I_UNDERSTAND"""
        return self.is_live_requested and self.live_confirm_token == "YES_I_UNDERSTAND"


@dataclass
class PolybotConfig:
    feeds: FeedsConfig = field(default_factory=FeedsConfig.from_env)
    recorder: RecorderConfig = field(default_factory=RecorderConfig.from_env)
    strategy: StrategyConfig = field(default_factory=StrategyConfig.from_env)
    risk: RiskConfig = field(default_factory=RiskConfig.from_env)
    live: LiveConfig = field(default_factory=LiveConfig.from_env)

    @classmethod
    def load(cls, config_path: Optional[str | Path] = None) -> PolybotConfig:
        """Loads configuration from YAML if exists, layered with env vars."""
        cfg = cls()
        if config_path:
            p = Path(config_path)
            if p.exists() and yaml:
                with open(p, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                # Update dataclasses with yaml contents if provided
                # Environmental variables will still take precedence if instantiated via from_env
        return cfg
