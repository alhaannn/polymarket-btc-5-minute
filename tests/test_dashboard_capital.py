"""
Unit tests for Capital Dashboard Endpoints and Monitoring (Roadmap Phase 7 Section B).
Verifies:
1. GET /api/capital returns authoritative breakdown: total equity, active, protected, budgets, failure counters, shutdown, Option B eligibility, reconciliation status.
2. GET /api/capital/transactions returns immutable audit trail.
3. GET /api/capital/reconciliations returns reconciliation history.
4. POST /api/capital/reconcile executes on-demand reconciliation audit.
5. Full synchronization between CapitalManager, CapitalReconciler, and Dashboard state.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from starlette.testclient import TestClient

from polybot.capital import CapitalManager, CapitalReconciler
from polybot.dashboard.server import create_dashboard_app
from polybot.dashboard.state import DashboardState
from polybot.execution.ledger import TradeLedger


@pytest.fixture
def dashboard_fixture(tmp_path: Path):
    db_file = tmp_path / "test_dash_ledger.db"
    capital_mgr = CapitalManager(db_path=db_file, starting_equity_usd=11.0)
    trade_ledger = TradeLedger(db_path=db_file)
    reconciler = CapitalReconciler(capital_manager=capital_mgr)
    dash_state = DashboardState()
    dash_state.update_capital(capital_mgr.state.to_dict())

    app = create_dashboard_app(
        state=dash_state,
        ledger=trade_ledger,
        capital_mgr=capital_mgr,
        reconciler=reconciler,
    )
    client = TestClient(app)
    yield client, capital_mgr, reconciler, dash_state


def test_get_capital_endpoint_returns_all_required_fields(dashboard_fixture):
    client, capital_mgr, _, _ = dashboard_fixture

    resp = client.get("/api/capital")
    assert resp.status_code == 200
    data = resp.json()

    # Section B required items:
    assert data["total_equity"] == 11.0
    assert data["active_capital"] == 11.0
    assert data["protected_capital"] == 0.0
    assert data["current_initial_budget"] == 1
    assert data["current_reversal_budget"] == 10
    assert "realized_net_pnl" in data
    assert "fees" in data
    assert "open_exposure" in data
    assert "reserved_collateral" in data
    assert "daily_failed_initial_count" in data
    assert "daily_failed_reversal_count" in data
    assert "daily_shutdown_status" in data
    assert "next_day_activation_eligible" in data
    assert "reconciliation_status" in data
    assert "spendable_active_funds" in data
    assert "active_funds_reserved" in data
    assert "protected_funds" in data
    assert "unsettled_funds" in data


def test_get_capital_transactions_endpoint(dashboard_fixture):
    client, capital_mgr, _, _ = dashboard_fixture

    # Initially has 1 transaction (INITIAL_CAPITAL)
    resp = client.get("/api/capital/transactions")
    assert resp.status_code == 200
    txs = resp.json()
    assert len(txs) == 1
    assert txs[0]["event_type"] == "INITIAL_CAPITAL"
    assert txs[0]["amount_cents"] == 1100

    # Process a trade settlement
    capital_mgr.process_settlement(
        settlement_id="test-settle-1",
        net_pnl_usd=5.0,
        fee_usd=0.05,
        trading_day="2026-11-01",
    )

    resp2 = client.get("/api/capital/transactions")
    assert resp2.status_code == 200
    txs2 = resp2.json()
    assert len(txs2) == 2
    assert txs2[0]["settlement_id"] == "test-settle-1"


def test_capital_reconciliation_endpoints(dashboard_fixture):
    client, capital_mgr, reconciler, dash_state = dashboard_fixture

    # POST to trigger reconciliation
    post_resp = client.post("/api/capital/reconcile")
    assert post_resp.status_code == 200
    res_data = post_resp.json()
    assert res_data["status"] == "ok"
    assert "report" in res_data
    assert res_data["report"]["is_valid"] is True
    assert res_data["report"]["status"] == "OK"

    # GET reconciliations history
    get_resp = client.get("/api/capital/reconciliations")
    assert get_resp.status_code == 200
    recon_list = get_resp.json()
    assert len(recon_list) >= 1
    assert recon_list[0]["status"] == "OK"
    assert recon_list[0]["expected_equity_cents"] == 1100


def test_state_endpoint_includes_rich_capital_dictionary(dashboard_fixture):
    client, _, _, _ = dashboard_fixture

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()

    assert "capital" in data
    cap = data["capital"]
    assert cap["total_equity"] == 11.0
    assert cap["active_capital"] == 11.0
    assert cap["protected_capital"] == 0.0
    assert cap["reconciliation_status"] == "OK"
