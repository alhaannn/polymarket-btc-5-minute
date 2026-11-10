import pytest
from pathlib import Path
from polybot.config import LiveConfig, PolybotConfig
from polybot.execution.base import Order
from polybot.execution.ledger import TradeLedger
from polybot.execution.live import LiveExecutionEngine


class MockClobClient:
    """Mock py-clob-client for offline testing without network access."""

    def __init__(self, positions=None):
        self._positions = positions or {}
        self.orders = []

    def get_positions(self):
        return self._positions

    def create_and_post_order(self, token_id, price=None, side=None, size=None, order_type=None):
        if isinstance(token_id, object) and hasattr(token_id, "token_id"):
            args = token_id
            self.orders.append({"token_id": args.token_id, "price": args.price, "side": args.side, "size": args.size})
        else:
            self.orders.append({"token_id": token_id, "price": price, "side": side, "size": size})
        return {"status": "FILLED", "order_id": "mock-ord-123", "orderID": "mock-ord-123"}

    def create_and_post_market_order(self, order_args, order_type=None):
        self.orders.append({
            "token_id": order_args.token_id,
            "price": order_args.price,
            "side": order_args.side,
            "amount": order_args.amount,
            "order_type": order_type,
        })
        taking_amt = str(round(order_args.amount / (order_args.price or 1.0), 4)) if order_args.side == "BUY" else str(order_args.amount)
        making_amt = str(order_args.amount) if order_args.side == "BUY" else str(round(order_args.amount * (order_args.price or 1.0), 2))
        return {
            "status": "matched",
            "order_id": "mock-ord-123",
            "orderID": "mock-ord-123",
            "takingAmount": taking_amt,
            "makingAmount": making_amt,
            "success": True,
        }

    def cancel(self, order_id):
        return True

    def redeem(self, condition_id):
        return True


def test_live_gate_rejection_without_credentials():
    cfg = PolybotConfig()
    cfg.live.is_live_requested = False
    cfg.live.live_confirm_token = ""

    # Must raise PermissionError
    with pytest.raises(PermissionError) as exc_info:
        LiveExecutionEngine(cfg)
    assert "ACCESS DENIED" in str(exc_info.value)


def test_live_gate_rejection_flag_only():
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "WRONG_TOKEN"

    with pytest.raises(PermissionError):
        LiveExecutionEngine(cfg)


def test_live_authorized_and_reconciliation_pass(tmp_path: Path):
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"

    mock_client = MockClobClient(positions={"token-1": 10.0})
    ledger_file = tmp_path / "ledger.json"

    # Engine startup without ledger file (clean state)
    engine = LiveExecutionEngine(cfg, clob_client=mock_client, ledger_path=ledger_file)
    assert engine.live_config.is_live_authorized()


def test_live_reconciliation_fails_closed_on_mismatch(tmp_path: Path):
    import json
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"

    ledger_file = tmp_path / "ledger.json"
    with open(ledger_file, "w") as f:
        json.dump({"token_balances": {"token-1": 50.0}}, f)

    # Server only has 10.0, ledger expects 50.0 -> must fail closed
    mock_client = MockClobClient(positions={"token-1": 10.0})
    with pytest.raises(RuntimeError) as exc:
        LiveExecutionEngine(cfg, clob_client=mock_client, ledger_path=ledger_file)
    assert "MISMATCH" in str(exc.value)


def test_micro_live_stake_cap_enforcement(tmp_path: Path):
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"
    cfg.live.micro_stake_cap_usd = 1.0

    mock_client = MockClobClient()
    test_ledger = TradeLedger(tmp_path / "test_micro.db")
    engine = LiveExecutionEngine(cfg, clob_client=mock_client, ledger=test_ledger)

    # Proposed order is $5.00 -> should be clamped to $1.00
    order = Order(
        order_id="ord-micro",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=5.0,
        shares=0.0,
        created_at_ts=1000.0,
    )
    res_order = engine.submit_order(order)
    # Clamped to $1.00 cap, then adjusted by floor ceiling to meet Polymarket >= $1.00 requirement
    assert 1.00 <= res_order.usd_amount <= 1.02
    assert res_order.status == "FILLED"


def test_live_reversal_stake_cap_allows_up_to_75(tmp_path: Path):
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"
    cfg.live.micro_stake_cap_usd = 75.0

    mock_client = MockClobClient()
    test_ledger = TradeLedger(tmp_path / "test_rev.db")
    engine = LiveExecutionEngine(cfg, clob_client=mock_client, ledger=test_ledger)

    order = Order(
        order_id="ord-rev",
        slug="btc-updown-5m-1000",
        side="Down",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=75.0,
        shares=0.0,
        created_at_ts=1000.0,
    )
    res_order = engine.submit_order(order)
    assert res_order.usd_amount == 75.0
    assert res_order.status == "FILLED"


def test_live_cancel_order_supports_both_api_methods(tmp_path: Path):
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"

    # Client with cancel_order (V2)
    class V2Client:
        def __init__(self):
            self.cancelled = []
        def cancel_order(self, oid):
            self.cancelled.append(oid)
            return True

    engine_v2 = LiveExecutionEngine(cfg, clob_client=V2Client(), ledger=TradeLedger(tmp_path / "test_v2.db"))
    assert engine_v2.cancel_order("ord-123") is True
    assert engine_v2.clob_client.cancelled == ["ord-123"]


def test_live_marketable_buy_strict_dollar_floor_precision(tmp_path: Path):
    """
    Polymarket CLOB enforces that market buy orders specify maker amount (USDC)
    with a maximum accuracy of 2 decimals ($1.00 USD minimum floor)
    and taker amount (shares) up to 4 decimals.
    """
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"

    for price in [0.890, 0.900, 0.910, 0.920]:
        mock_client = MockClobClient()
        engine = LiveExecutionEngine(cfg, clob_client=mock_client, ledger=TradeLedger(tmp_path / f"test_{int(price*1000)}.db"))
        unrounded_shares = 1.0 / price
        order = Order(
            order_id=f"ord-{int(price*1000)}",
            slug="btc-updown-5m-1000",
            side="Up",
            action="BUY",
            order_type="taker",
            price=price,
            usd_amount=1.0,
            shares=unrounded_shares,
            created_at_ts=1000.0,
        )
        res_order = engine.submit_order(order)
        # Order shares must be accurate within 4 decimals (Polymarket taker spec)
        assert round(res_order.shares, 4) == res_order.shares
        # Maker cash amount must be at least $1.00 and have at most 2 decimals
        assert res_order.usd_amount >= 1.00
        assert round(res_order.usd_amount, 2) == res_order.usd_amount
        # Notional value must be strictly >= $1.00 USD
        notional = round(res_order.shares * price, 4)
        assert notional >= 0.9999, f"Order at {price} had sub-dollar notional {notional} < $1.00"


    # Client with cancel (V1 legacy)
    class V1Client:
        def __init__(self):
            self.cancelled = []
        def cancel(self, oid):
            self.cancelled.append(oid)
            return True

    engine_v1 = LiveExecutionEngine(cfg, clob_client=V1Client(), ledger=TradeLedger(tmp_path / "test_v1.db"))
    assert engine_v1.cancel_order("ord-456") is True
    assert engine_v1.clob_client.cancelled == ["ord-456"]


def test_live_order_minimum_floor_enforcement(tmp_path: Path):
    """Ensure orders with float rounding never fall below Polymarket's $1.00 USD floor."""
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"
    cfg.live.micro_stake_cap_usd = 75.0

    mock_client = MockClobClient()
    test_ledger = TradeLedger(tmp_path / "test_floor.db")
    engine = LiveExecutionEngine(cfg, clob_client=mock_client, ledger=test_ledger)

    # Price 0.89: 1.00 / 0.89 = 1.12359.
    # Standard 2-decimal round gives 1.12 -> 1.12 * 0.89 = $0.9968 (rejected by CLOB).
    # Ceiling logic must bump size to 1.13 -> 1.13 * 0.89 = $1.0057 >= $1.00.
    order_89 = Order(
        order_id="ord-floor-89",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        price=0.89,
        usd_amount=1.00,
        shares=0.0,
        created_at_ts=1000.0,
    )
    res_89 = engine.submit_order(order_89)
    assert res_89.status == "FILLED"
    assert res_89.shares >= 1.12
    assert res_89.usd_amount == 1.00
    assert round(res_89.shares * res_89.price, 4) >= 0.9999

    # Price 0.90: Market order specifies amount=$1.00, getting >= 1.11 shares
    order_90 = Order(
        order_id="ord-floor-90",
        slug="btc-updown-5m-1000",
        side="Down",
        action="BUY",
        order_type="taker",
        price=0.90,
        usd_amount=1.00,
        shares=0.0,
        created_at_ts=1000.0,
    )
    res_90 = engine.submit_order(order_90)
    assert res_90.status == "FILLED"
    assert res_90.shares >= 1.11
    assert res_90.usd_amount == 1.00
    assert round(res_90.shares * res_90.price, 4) >= 0.9999


def test_clob_v2_order_builder_truncation_guard():
    """Verify that the float epsilon prevents py_clob_client_v2 IEEE-754 floor truncation."""
    try:
        from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG, OrderBuilder
    except ImportError:
        return

    b = OrderBuilder.__new__(OrderBuilder)
    # Price 0.89: 1.13 shares should produce makerAmount >= $1.0057 (1005700) and not 0.9968 (996800)
    side, maker_amt, taker_amt = b.get_order_amounts("BUY", 1.13 + 1e-6, 0.89, ROUNDING_CONFIG["0.01"])
    assert maker_amt >= 1_000_000, f"maker_amt {maker_amt} fell below $1.00 minimum!"
    assert taker_amt == 1_130_000, f"taker_amt {taker_amt} truncated below 1.13 shares!"



