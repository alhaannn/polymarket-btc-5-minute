"""
Unit tests for Multi-Market Architecture:
- Compartmentalized MarketCell ($12 baseline, isolated sizing, reversal bulkheads)
- Autonomous Reviver & Dividend Harvest engine ($76 when 1 halted, $88 when 0 halted)
- GlobalConcurrencyArbiter (equity-scaled slots, collision prevention)
- BinanceFeed multiplex stream parsing
- PolymarketClient multi-asset resolution
"""
import pytest
from polybot.capital.cell import MarketCell, MarketCellManager
from polybot.multi_market import GlobalConcurrencyArbiter, MultiMarketOrchestrator
from polybot.feeds.binance import BinanceFeed
from polybot.gamma import PolymarketClient


class TestMarketCell:
    def test_market_cell_initial_state(self):
        cell = MarketCell(asset="btc", initial_capital_cents=1200)
        assert cell.capital_usd == 12.00
        assert cell.get_initial_budget() == 1  # 12 / 11 = 1.09 -> 1
        assert cell.get_reversal_budget() == 11  # 12 * 10 / 11 = 10.9 -> 11
        assert cell.status == "ACTIVE"
        assert cell.is_active() is True
        assert cell.progress_pct == 12.0

    def test_market_cell_win_compounding(self):
        cell = MarketCell(asset="eth", initial_capital_cents=1200)
        # Win +$12.00 (1200 cents)
        crossed = cell.record_win(1200)
        assert crossed is False
        assert cell.capital_usd == 24.00
        assert cell.get_initial_budget() == 2  # 24 / 11 = 2.18 -> 2
        assert cell.get_reversal_budget() == 22  # 24 * 10 / 11 = 21.8 -> 22
        assert cell.trades_count == 1
        assert cell.wins_count == 1

    def test_market_cell_reversal_loss_bulkhead(self):
        cell = MarketCell(asset="sol", initial_capital_cents=1200)
        # Catastrophic reversal loss
        cell.record_reversal_loss(1200)
        assert cell.capital_usd == 0.00
        assert cell.status == "HALTED_DEPLETED"
        assert cell.is_active() is False
        assert cell.get_initial_budget() == 0
        assert cell.get_reversal_budget() == 0

    def test_market_cell_revival(self):
        cell = MarketCell(asset="doge", initial_capital_cents=1200)
        cell.record_reversal_loss(1200)
        assert cell.is_active() is False
        # Revive with $12 grant
        cell.revive(grant_cents=1200)
        assert cell.is_active() is True
        assert cell.status == "ACTIVE"
        assert cell.capital_usd == 12.00
        assert cell.get_initial_budget() == 1


class TestMarketCellManagerAndReviver:
    def test_cell_manager_fleet_initialization(self):
        mgr = MarketCellManager()
        assert mgr.total_fleet_equity_usd == 72.00  # 6 * $12
        assert mgr.active_cells_count == 6
        assert mgr.halted_cells_count == 0

    def test_harvest_with_zero_halted_markets(self):
        mgr = MarketCellManager()
        btc = mgr.get_cell("btc")
        # BTC reaches $100.00
        btc.capital_cents = 10000

        event = mgr.check_and_execute_harvest("btc")
        assert event is not None
        assert event.winning_asset == "BTC"
        assert event.self_seed_usd == 12.00
        assert event.halted_revived_assets == []
        assert event.revival_funds_usd == 0.00
        # Sweep = $100 - $12 = $88.00
        assert event.sweep_amount_usd == 88.00
        assert btc.capital_usd == 12.00  # Reset to $12
        assert btc.status == "ACTIVE"

    def test_harvest_with_one_halted_market_user_plan(self):
        """Tests the user's exact scenario: 1 market halted, 1 hits $100 -> sweep $76!"""
        mgr = MarketCellManager()
        sol = mgr.get_cell("sol")
        btc = mgr.get_cell("btc")

        # SOL wipes out
        sol.record_reversal_loss(1200)
        assert mgr.halted_cells_count == 1
        assert "sol" in mgr.halted_cells

        # BTC hits $100.00
        btc.capital_cents = 10000

        event = mgr.check_and_execute_harvest("btc")
        assert event is not None
        assert event.winning_asset == "BTC"
        assert event.self_seed_usd == 12.00
        assert event.halted_revived_assets == ["SOL"]
        assert event.revival_funds_usd == 12.00
        # Sweep = $100 - $12 (self) - $12 (SOL) = $76.00
        assert event.sweep_amount_usd == 76.00

        # Verify SOL is revived to $12 and ACTIVE!
        assert sol.capital_usd == 12.00
        assert sol.status == "ACTIVE"
        assert sol.is_active() is True
        assert btc.capital_usd == 12.00
        assert mgr.halted_cells_count == 0
        assert mgr.active_cells_count == 6

    def test_harvest_with_two_halted_markets(self):
        mgr = MarketCellManager()
        sol = mgr.get_cell("sol")
        doge = mgr.get_cell("doge")
        eth = mgr.get_cell("eth")

        sol.record_reversal_loss(1200)
        doge.record_reversal_loss(1200)
        assert mgr.halted_cells_count == 2

        eth.capital_cents = 10000
        event = mgr.check_and_execute_harvest("eth")
        assert event is not None
        # Sweep = $100 - $12 - 24 = $64.00
        assert event.sweep_amount_usd == 64.00
        assert sol.is_active() is True
        assert doge.is_active() is True
        assert mgr.halted_cells_count == 0


class TestGlobalConcurrencyArbiter:
    def test_concurrency_slots_scaling(self):
        arb = GlobalConcurrencyArbiter()
        assert arb.get_max_slots(12.0) == 1
        assert arb.get_max_slots(45.0) == 1
        assert arb.get_max_slots(72.0) == 2
        assert arb.get_max_slots(160.0) == 3
        assert arb.get_max_slots(350.0) == 4

    def test_slot_acquisition_and_locking(self):
        arb = GlobalConcurrencyArbiter()
        # Fleet equity = $40.0 (Max slots = 1)
        assert arb.can_acquire("btc", 40.0) is True
        assert arb.acquire("btc", "INITIAL", 1.0, 40.0) is True

        # Second market denied
        assert arb.can_acquire("eth", 40.0) is False
        assert arb.acquire("eth", "INITIAL", 1.0, 40.0) is False
        assert "ETH" in arb.queued_assets

        # Release BTC slot
        arb.release("btc")
        assert arb.can_acquire("eth", 40.0) is True
        assert arb.acquire("eth", "INITIAL", 1.0, 40.0) is True


class TestBinanceMultiplexFeed:
    def test_multiplex_trade_message_parsing(self):
        feed = BinanceFeed(symbols=["btcusdt", "ethusdt"])
        msg = (
            '{"stream":"ethusdt@trade",'
            '"data":{"e":"trade","E":1731175000000,"s":"ETHUSDT","t":9876,"p":"2875.40","q":"1.5","T":1731175000000,"m":true}}'
        )
        feed._process_message(msg)
        assert feed.get_last_price("eth") == 2875.40
        assert feed.last_tick["asset"] == "eth"
        assert feed.last_tick["symbol"] == "ETHUSDT"


class TestPolymarketClientMultiAsset:
    def test_get_market_slug_generation(self):
        client = PolymarketClient()
        # Mock get_event to inspect slug passed
        called_slug = None
        def mock_get_event(slug):
            nonlocal called_slug
            called_slug = slug
            return None
        client.get_event = mock_get_event

        client.get_market(window_start=1731175200, asset="sol")
        assert called_slug == "sol-updown-5m-1731175200"

        client.get_market(window_start=1731175200, asset="doge")
        assert called_slug == "doge-updown-5m-1731175200"

