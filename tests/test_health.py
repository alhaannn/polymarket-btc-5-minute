import time
from polybot.feeds.health import FeedHealthTracker


def test_health_staleness():
    tracker = FeedHealthTracker(max_staleness_sec=1.0)
    now_ms = int(time.time() * 1000)

    # Initial state is stale
    assert tracker.is_chainlink_stale()

    # Record tick
    tracker.record_tick("chainlink", 80000.0, now_ms)
    assert not tracker.is_chainlink_stale()

    # Simulate passage of time
    tracker.chainlink.last_received_local_ts -= 2.0
    assert tracker.is_chainlink_stale()
    healthy, reason = tracker.is_healthy()
    assert not healthy
    assert "stale" in reason.lower()


def test_health_gap_detection():
    tracker = FeedHealthTracker(gap_threshold_sec=5.0)
    t0 = 1700000000000
    gap1 = tracker.record_tick("chainlink", 80000.0, t0)
    assert gap1 is None

    # Tick 3 seconds later - no gap
    gap2 = tracker.record_tick("chainlink", 80010.0, t0 + 3000)
    assert gap2 is None

    # Tick 6 seconds later - gap detected
    gap3 = tracker.record_tick("chainlink", 80020.0, t0 + 9000)
    assert gap3 is not None
    assert gap3.gap_duration_sec == 6.0
    assert len(tracker.chainlink.gaps) == 1


def test_health_divergence():
    tracker = FeedHealthTracker(max_divergence_pct=0.01)  # 1%
    now_ms = int(time.time() * 1000)

    tracker.record_tick("chainlink", 80000.0, now_ms)
    tracker.record_tick("binance", 80200.0, now_ms)  # 0.25% diff

    div, is_div = tracker.get_divergence()
    assert div is not None
    assert not is_div

    # Divergence > 1% (e.g. 80000 vs 81500 = 1.875%)
    tracker.record_tick("binance", 81500.0, now_ms)
    div, is_div = tracker.get_divergence()
    assert is_div
    healthy, reason = tracker.is_healthy()
    assert not healthy
    assert "divergence" in reason.lower()
