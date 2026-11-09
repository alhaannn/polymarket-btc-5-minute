"""
Unit tests for Automatic Live Wallet Rebalancing (On-Chain Deposits & Withdrawals).
Verifies:
1. Wallet balance changes on Polymarket automatically trigger proportional rebalancing.
2. Post-Milestone 1 ($44+): Balances automatically rebalance 50/50 into Active and Protected Vault.
3. User scenario: $150 balance with sudden $69 withdrawal (down to $81) rebalances cleanly to $40.50 / $40.50 ($4 Init / $37 Rev).
4. Drops below $44 assign 100% to Active capital with $0 Protected.
5. Invariants hold across all deposits and withdrawals.
"""
import pytest
import tempfile
from pathlib import Path
from polybot.capital.manager import CapitalManager


def test_live_wallet_deposit_and_withdrawal_rebalancing():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_rebalance.db"
        cm = CapitalManager(db_path=db_path, starting_equity_usd=11.0, sizing_mode="dynamic")

        # Initial $11.00 starting state
        assert cm.state.total_equity == 11.0
        assert cm.state.active_capital == 11.0
        assert cm.state.protected_capital == 0.0
        assert cm.get_trade_budgets() == (1, 10)

        # 1. User deposits funds into Polymarket wallet bringing balance to $150.00
        cm.sync_live_wallet_balance(150.00, auto_rebalance=True)
        assert cm.state.total_equity == 150.0
        assert cm.state.active_capital == 75.0
        assert cm.state.protected_capital == 75.0
        assert cm.state.milestone_44_activated
        # Active $75: Dynamic sizing 8% = $6.00 Initial, 4x = $24.00 Reversal
        assert cm.get_trade_budgets() == (6, 24)

        # 2. User's exact scenario: Withdraw $69 from Polymarket wallet (down to $81.00)
        cm.sync_live_wallet_balance(81.00, auto_rebalance=True)
        assert cm.state.total_equity == 81.0
        # 50/50 split of 8100 cents: 4050 cents ($40.50) Active, 4050 cents ($40.50) Protected
        assert cm.state.active_capital == 40.50
        assert cm.state.protected_capital == 40.50
        assert cm.state.total_equity == cm.state.active_capital + cm.state.protected_capital
        # Sizing at $40.50 Active (<= $50 threshold -> canonical regime):
        # Initial: round(40.50 / 11) = $4.00
        # Reversal: round(40.50 * 10 / 11) = $37.00
        assert cm.get_trade_budgets() == (4, 37)

        # 3. User withdraws down to $25.00 (below $44 milestone threshold)
        cm.sync_live_wallet_balance(25.00, auto_rebalance=True)
        assert cm.state.total_equity == 25.0
        assert cm.state.active_capital == 25.0
        assert cm.state.protected_capital == 0.0
        assert not cm.state.milestone_44_activated
        # Sizing at $25: round(25/11) = $2.00, round(250/11) = $23.00
        assert cm.get_trade_budgets() == (2, 23)

        # 4. User deposits large capital ($1,000.00 total)
        cm.sync_live_wallet_balance(1000.00, auto_rebalance=True)
        assert cm.state.total_equity == 1000.0
        assert cm.state.active_capital == 500.0
        assert cm.state.protected_capital == 500.0
        assert cm.state.milestone_44_activated
        # Active $500: Capped at $20.00 Initial and $75.00 Reversal!
        assert cm.get_trade_budgets() == (20, 75)

