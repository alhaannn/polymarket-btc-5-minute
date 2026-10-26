import pytest
from pathlib import Path
from polybot.config import LiveConfig, PolybotConfig
from polybot.execution.base import Order
from polybot.execution.live import LiveExecutionEngine


class MockClobClient:
    """Mock py-clob-client for offline testing without network access."""

    def __init__(self, positions=None):
        self._positions = positions or {}
        self.orders = []

    def get_positions(self):
        return self._positions

    def create_and_post_order(self, token_id, price, side, size, order_type):
        self.orders.append({"token_id": token_id, "price": price, "side": side, "size": size})
        return {"status": "FILLED", "order_id": "mock-ord-123"}

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


def test_micro_live_stake_cap_enforcement():
    cfg = PolybotConfig()
    cfg.live.is_live_requested = True
    cfg.live.live_confirm_token = "YES_I_UNDERSTAND"
    cfg.live.micro_stake_cap_usd = 1.0

    mock_client = MockClobClient()
    engine = LiveExecutionEngine(cfg, clob_client=mock_client)

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
    assert res_order.usd_amount == 1.0
    assert res_order.status == "FILLED"
