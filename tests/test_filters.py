import time
import pytest
from polybot.config import StrategyConfig
from polybot.feeds.clob_ws import TokenOrderBook
from polybot.feeds.health import FeedHealthTracker
from polybot.filters import FilterEngine


def test_filter_lead_size():
    cfg = StrategyConfig(z_min=1.5, min_lead_usd=10.0)
    engine = FilterEngine(cfg)

    # Lead too small in dollars
    err1 = engine.check_lead_size(z_score=2.0, lead_usd=5.0)
    assert err1 is not None
    assert "dollars" in err1.lower()

    # Z-score too low
    err2 = engine.check_lead_size(z_score=1.2, lead_usd=20.0)
    assert err2 is not None
    assert "z-score" in err2.lower()

    # Passes both
    assert engine.check_lead_size(z_score=1.8, lead_usd=20.0) is None


def test_filter_adverse_drift():
    cfg = StrategyConfig(adverse_drift_max_pct=0.40)  # max 40% erosion
    engine = FilterEngine(cfg)

    t0 = 100.0
    engine.record_lead(t0, lead_usd=50.0)
    engine.record_lead(t0 + 15.0, lead_usd=45.0)
    # 30s later lead eroded from 50 to 25 (50% erosion)
    engine.record_lead(t0 + 30.0, lead_usd=25.0)

    err = engine.check_adverse_drift(side="Up", window_sec=30.0)
    assert err is not None
    assert "adverse drift" in err.lower()


def test_filter_vol_spike():
    cfg = StrategyConfig(vol_spike_ratio_max=2.0)
    engine = FilterEngine(cfg)

    assert engine.check_vol_spike(vol_spike_ratio=1.5) is None
    err = engine.check_vol_spike(vol_spike_ratio=2.5)
    assert err is not None
    assert "vol spike" in err.lower()


def test_filter_order_book_liquidity():
    cfg = StrategyConfig(spread_max=0.05, max_slippage=0.02)
    engine = FilterEngine(cfg)

    book = TokenOrderBook("token-1")
    # Spread too wide (0.40 bid vs 0.50 ask -> spread 0.10 > 0.05)
    book.apply_snapshot([{"price": "0.40", "size": "100"}], [{"price": "0.50", "size": "100"}])

    err, fill = engine.check_order_book(book, stake_usd=5.0)
    assert err is not None
    assert "spread" in err.lower()

    # Tight spread (0.48 bid vs 0.50 ask -> spread 0.02)
    book.apply_snapshot([{"price": "0.48", "size": "100"}], [{"price": "0.50", "size": "100"}])
    err_ok, fill_ok = engine.check_order_book(book, stake_usd=5.0)
    assert err_ok is None
    assert fill_ok is not None


def test_filter_feed_health():
    engine = FilterEngine(StrategyConfig())
    health = FeedHealthTracker(max_staleness_sec=1.0)
    # Uninitialized health -> stale
    err = engine.check_feed_health(health)
    assert err is not None
    assert "unhealthy" in err.lower()
