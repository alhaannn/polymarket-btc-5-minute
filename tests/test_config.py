import os
from polybot.config import PolybotConfig, LiveConfig


def test_config_defaults():
    cfg = PolybotConfig()
    assert cfg.feeds.max_feed_staleness_sec == 3.0
    assert cfg.strategy.stake_usd == 5.0
    assert cfg.strategy.min_edge == 0.02
    assert cfg.risk.daily_loss_limit_usd == 25.0
    assert not cfg.live.is_live_authorized()


def test_live_gate_authorization():
    # Neither flag nor confirm
    live = LiveConfig(is_live_requested=False, live_confirm_token="")
    assert not live.is_live_authorized()

    # Flag only
    live_flag_only = LiveConfig(is_live_requested=True, live_confirm_token="")
    assert not live_flag_only.is_live_authorized()

    # Token only
    live_token_only = LiveConfig(is_live_requested=False, live_confirm_token="YES_I_UNDERSTAND")
    assert not live_token_only.is_live_authorized()

    # Both flag and token present
    live_authorized = LiveConfig(is_live_requested=True, live_confirm_token="YES_I_UNDERSTAND")
    assert live_authorized.is_live_authorized()


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("STAKE_USD", "12.5")
    monkeypatch.setenv("MAX_FEED_STALENESS_SEC", "2.5")
    monkeypatch.setenv("MIN_EDGE", "0.035")

    cfg = PolybotConfig.load()
    assert cfg.strategy.stake_usd == 12.5
    assert cfg.feeds.max_feed_staleness_sec == 2.5
    assert cfg.strategy.min_edge == 0.035
