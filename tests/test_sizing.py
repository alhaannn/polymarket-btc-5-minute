import pytest
from polybot.config import RiskConfig
from polybot.sizing import PositionSizer


def test_fixed_sizing():
    sizer = PositionSizer(RiskConfig(sizing_mode="fixed"), default_stake_usd=5.0)
    stake = sizer.calculate_stake(edge=0.05, model_prob=0.95, price=0.90, bankroll_usd=100.0)
    assert stake == 5.0


def test_fractional_kelly_sizing():
    cfg = RiskConfig(
        sizing_mode="fractional_kelly",
        kelly_fraction=0.25,
        kelly_max_stake_usd=15.0,
    )
    sizer = PositionSizer(cfg)

    # Edge = 0.05, Price = 0.90
    # Full Kelly = edge / (1 - price) = 0.05 / 0.10 = 0.50 (50% of bankroll)
    # Quarter-Kelly = 0.50 * 0.25 = 0.125 (12.5% of bankroll)
    # On $100 bankroll -> $12.50 stake
    stake = sizer.calculate_stake(edge=0.05, model_prob=0.95, price=0.90, bankroll_usd=100.0)
    assert pytest.approx(stake) == 12.50

    # On $200 bankroll -> 0.125 * 200 = $25.00 -> should cap at kelly_max_stake_usd ($15.0)
    stake_capped = sizer.calculate_stake(edge=0.05, model_prob=0.95, price=0.90, bankroll_usd=200.0)
    assert stake_capped == 15.0

    # Negative or zero edge -> returns 0
    stake_no_edge = sizer.calculate_stake(edge=0.0, model_prob=0.90, price=0.90, bankroll_usd=100.0)
    assert stake_no_edge == 0.0
