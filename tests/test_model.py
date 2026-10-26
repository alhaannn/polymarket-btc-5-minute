import math
import pytest
from polybot.model import ProbabilityModel, RollingEWMAVol, standard_normal_cdf


def test_standard_normal_cdf():
    assert pytest.approx(standard_normal_cdf(0.0)) == 0.5
    assert pytest.approx(standard_normal_cdf(1.96), abs=1e-3) == 0.975
    assert pytest.approx(standard_normal_cdf(-1.96), abs=1e-3) == 0.025


def test_brownian_probability_model():
    model = ProbabilityModel()
    strike_K = 80000.0
    vol = 0.0002  # per sec (~1.2% per hour)
    time_left = 60.0  # 1 minute left

    # Up side with positive lead (price = 80050, K = 80000)
    out_up = model.compute_probability(
        current_price=80050.0,
        strike_price_K=strike_K,
        time_left_sec=time_left,
        vol_per_sec=vol,
        side="Up",
    )
    assert out_up.lead_usd == 50.0
    assert out_up.z_score > 0
    assert out_up.p_raw > 0.5
    assert out_up.p_calibrated > 0.5

    # Down side with same price should have negative lead
    out_down = model.compute_probability(
        current_price=80050.0,
        strike_price_K=strike_K,
        time_left_sec=time_left,
        vol_per_sec=vol,
        side="Down",
    )
    assert out_down.lead_usd == -50.0
    assert out_down.z_score < 0
    assert out_down.p_raw < 0.5
    assert out_down.p_calibrated < 0.5


def test_tie_rule_on_strike():
    model = ProbabilityModel()
    strike_K = 80000.0
    # Price exactly at strike
    out_up = model.compute_probability(
        current_price=80000.0,
        strike_price_K=strike_K,
        time_left_sec=10.0,
        vol_per_sec=0.0001,
        side="Up",
    )
    # Tie rule: end >= start resolves Up
    assert out_up.p_raw >= 0.50


def test_rolling_ewma_vol():
    ewma = RollingEWMAVol(short_span=10, long_span=30)
    t = 1700000000.0
    p = 80000.0

    ewma.update(p, t)
    assert not ewma.initialized

    # Feed a series of stable prices
    for i in range(1, 15):
        t += 1.0
        p += 5.0  # tiny moves
        ewma.update(p, t)

    assert ewma.initialized
    assert ewma.get_short_vol_per_sec() > 0
    assert ewma.get_long_vol_per_sec() > 0
    assert pytest.approx(ewma.get_vol_spike_ratio(), abs=0.5) == 1.0

    # Inject sudden volatility burst
    for i in range(5):
        t += 1.0
        p += 150.0 if i % 2 == 0 else -150.0
        ewma.update(p, t)

    # Short vol should now exceed long vol
    assert ewma.get_short_vol_per_sec() > ewma.get_long_vol_per_sec()
    assert ewma.get_vol_spike_ratio() > 1.2


def test_brier_score_metric():
    # Perfect predictions
    preds = [1.0, 0.0, 1.0, 0.0]
    actuals = [1, 0, 1, 0]
    assert ProbabilityModel.calculate_brier_score(preds, actuals) == 0.0

    # Worst possible predictions
    preds_bad = [0.0, 1.0, 0.0, 1.0]
    assert ProbabilityModel.calculate_brier_score(preds_bad, actuals) == 1.0

    # Coin flip 0.5 predictions
    preds_50 = [0.5, 0.5, 0.5, 0.5]
    assert pytest.approx(ProbabilityModel.calculate_brier_score(preds_50, actuals)) == 0.25
