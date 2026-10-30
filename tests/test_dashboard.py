import pytest
from starlette.testclient import TestClient

from polybot.dashboard.server import create_dashboard_app
from polybot.dashboard.state import DashboardState
from polybot.execution.ledger import TradeLedger


@pytest.fixture
def client_and_state(tmp_path):
    ledger = TradeLedger(tmp_path / "dashboard_test_ledger.db")
    state = DashboardState()
    state.set_bot_status("RUNNING")
    state.update_clock("btc-updown-5m-1000", 1000, 1300, 185.0, 115.0, "ACTIVE", True)
    state.update_probabilities(up_prob=0.905, down_prob=0.095, up_bid=0.895, down_bid=0.085, latency_ms=12.5)

    app = create_dashboard_app(state, ledger)
    client = TestClient(app)
    return client, state, ledger


def test_dashboard_index_html(client_and_state):
    client, state, ledger = client_and_state
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Polymarket" in resp.text
    assert "Workstation" in resp.text


def test_dashboard_api_state(client_and_state):
    client, state, ledger = client_and_state
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bot_status"] == "RUNNING"
    assert data["current_slug"] == "btc-updown-5m-1000"
    assert data["phase"] == "ACTIVE"
    assert data["is_trading_allowed"] is True
    assert data["up_prob"] == 0.905
    assert data["down_prob"] == 0.095
    assert data["latency_ms"] == 12.5


def test_dashboard_api_performance_and_trades(client_and_state):
    client, state, ledger = client_and_state
    # Add a mock trade in ledger
    ledger.record_trade(
        order_id="test-ord-1",
        slug="btc-updown-5m-1000",
        side="Up",
        action="BUY",
        order_type="taker",
        signal_type="INITIAL",
        stake_usd=1.0,
        shares=1.1,
        avg_price=0.90,
        fee_usd=0.001,
        exec_ts=1000.0,
        status="OPEN",
    )

    resp_trades = client.get("/api/trades")
    assert resp_trades.status_code == 200
    trades = resp_trades.json()
    assert len(trades) == 1
    assert trades[0]["order_id"] == "test-ord-1"

    resp_perf = client.get("/api/performance")
    assert resp_perf.status_code == 200
    perf = resp_perf.json()
    assert "lifetime" in perf
    assert perf["lifetime"]["total_trades"] == 1


def test_dashboard_api_control(client_and_state):
    client, state, ledger = client_and_state
    resp = client.post("/api/control", json={"action": "pause"})
    assert resp.status_code == 200
    assert state.pop_requested_action() == "PAUSE"
