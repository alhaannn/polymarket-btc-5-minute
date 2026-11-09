"""
Local Web Dashboard Server powered by Starlette and Uvicorn.
Provides real-time REST endpoints, authentication, and serves the light-themed financial workstation UI.
Runs non-blockingly alongside the trading bot in the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from polybot.dashboard.auth import AuthManager
from polybot.dashboard.state import DashboardState
from polybot.execution.ledger import TradeLedger

log = logging.getLogger("polybot.dashboard.server")

STATIC_DIR = Path(__file__).parent / "static"


def get_client_ip(request: Request) -> str:
    """Extracts client IP from proxy headers or direct socket connection."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "127.0.0.1"


def create_dashboard_app(
    state: DashboardState,
    ledger: TradeLedger,
    capital_mgr: Optional[Any] = None,
    reconciler: Optional[Any] = None,
    auth_mgr: Optional[AuthManager] = None,
) -> Starlette:
    """Builds Starlette application for the trading dashboard."""

    def secure_response(resp: Response) -> Response:
        """Injects defense-in-depth security headers on all responses."""
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-XSS-Protection"] = "1; mode=block"
        return resp

    def check_auth(request: Request) -> Optional[Response]:
        """Validates active session when auth_mgr is enabled."""
        if not (auth_mgr and auth_mgr.enabled):
            return None
        token = request.cookies.get("session_id")
        if not token:
            auth_hdr = request.headers.get("authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        username = auth_mgr.validate_session(token)
        if not username:
            if request.url.path.startswith("/api/"):
                return secure_response(JSONResponse({"status": "error", "message": "Authentication required"}, status_code=401))
            return secure_response(RedirectResponse(url="/login", status_code=303))
        return None

    async def get_index(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        index_file = STATIC_DIR / "index.html"
        return secure_response(FileResponse(index_file))

    async def get_login(request: Request) -> Response:
        if auth_mgr and auth_mgr.enabled:
            token = request.cookies.get("session_id")
            if token and auth_mgr.validate_session(token):
                return secure_response(RedirectResponse(url="/", status_code=303))
        login_file = STATIC_DIR / "login.html"
        return secure_response(FileResponse(login_file))

    async def post_auth_login(request: Request) -> Response:
        if not (auth_mgr and auth_mgr.enabled):
            return secure_response(JSONResponse({"status": "ok", "message": "Authentication disabled"}))

        try:
            body = await request.json()
        except Exception:
            return secure_response(JSONResponse({"status": "error", "message": "Invalid request body"}, status_code=400))

        username = body.get("username", "")
        password = body.get("password", "")
        ip = get_client_ip(request)

        ok, err = auth_mgr.verify_login(username, password, ip=ip)
        if not ok:
            return secure_response(JSONResponse({"status": "error", "message": err}, status_code=401))

        token = auth_mgr.create_session(username)
        resp = JSONResponse({"status": "ok", "username": username})

        is_secure = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
        )
        resp.set_cookie(
            key="session_id",
            value=token,
            max_age=auth_mgr.session_expiry_sec,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            path="/",
        )
        return secure_response(resp)

    async def post_auth_logout(request: Request) -> Response:
        token = request.cookies.get("session_id")
        if not token:
            auth_hdr = request.headers.get("authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        if token and auth_mgr:
            auth_mgr.invalidate_session(token)
        resp = JSONResponse({"status": "ok", "message": "Logged out successfully"})
        resp.delete_cookie("session_id", path="/")
        return secure_response(resp)

    async def get_auth_me(request: Request) -> JSONResponse:
        if not (auth_mgr and auth_mgr.enabled):
            return secure_response(JSONResponse({
                "authenticated": True,
                "auth_enabled": False,
                "pin_required": False,
                "username": "anonymous",
            }))
        token = request.cookies.get("session_id")
        if not token:
            auth_hdr = request.headers.get("authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        username = auth_mgr.validate_session(token)
        if username:
            return secure_response(JSONResponse({
                "authenticated": True,
                "auth_enabled": True,
                "pin_required": bool(auth_mgr.pin_hash),
                "username": username,
            }))
        return secure_response(JSONResponse({
            "authenticated": False,
            "auth_enabled": True,
            "pin_required": bool(auth_mgr.pin_hash),
        }, status_code=401))

    async def get_state(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        state_dict = state.to_dict()
        shutdown_active = capital_mgr.is_daily_shutdown_active() if capital_mgr else False
        shutdown_until = capital_mgr.state.shutdown_until_ts if capital_mgr else None
        recent_trades = ledger.get_recent_trades(limit=500) if ledger else None
        from polybot.tz import compute_schedule_state
        sched = compute_schedule_state(
            prime_hours_only=getattr(state, "prime_hours_only", True),
            shutdown_active=shutdown_active,
            shutdown_until_ts=shutdown_until,
            trades=recent_trades,
        )
        state_dict["schedule"] = sched
        state.update_schedule_state(sched)
        return secure_response(JSONResponse(state_dict))

    async def get_performance(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        summary = ledger.get_performance_summary()
        return secure_response(JSONResponse(summary))

    async def get_trades(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        limit = int(request.query_params.get("limit", 1000))
        trades = ledger.get_recent_trades(limit=limit)
        return secure_response(JSONResponse(trades))

    async def get_logs(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        limit = int(request.query_params.get("limit", 1000))
        return secure_response(JSONResponse(list(state.logs)[:limit]))

    async def export_trades_csv(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        trades = ledger.get_recent_trades(limit=1000)
        output = io.StringIO()
        if trades:
            writer = csv.DictWriter(output, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        else:
            output.write("order_id,slug,status,pnl_usd\n")

        resp = Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=paper_trades.csv"},
        )
        return secure_response(resp)

    async def get_capital(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        cap_dict = dict(state.capital_state)
        if capital_mgr:
            cap_dict.update(capital_mgr.state.to_dict())
            init_b, rev_b = capital_mgr.get_trade_budgets()
            cap_dict["current_initial_budget"] = init_b
            cap_dict["current_reversal_budget"] = rev_b
        cap_dict["execution_mode"] = getattr(state, "execution_mode", "paper")
        return secure_response(JSONResponse(cap_dict))

    async def get_capital_transactions(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        limit = int(request.query_params.get("limit", 1000))
        if capital_mgr and hasattr(capital_mgr, "ledger"):
            txs = capital_mgr.ledger.get_recent_transactions(limit=limit)
            return secure_response(JSONResponse(txs))
        return secure_response(JSONResponse([]))

    async def get_capital_reconciliations(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        limit = int(request.query_params.get("limit", 50))
        if capital_mgr and hasattr(capital_mgr, "ledger"):
            recons = capital_mgr.ledger.get_recent_reconciliations(limit=limit)
            return secure_response(JSONResponse(recons))
        return secure_response(JSONResponse([]))

    async def post_capital_reconcile(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        if reconciler:
            report = reconciler.reconcile()
            if capital_mgr:
                state.update_capital(capital_mgr.state.to_dict())
            return secure_response(JSONResponse({"status": "ok", "report": report.to_dict()}))
        return secure_response(JSONResponse({"status": "ok", "message": "No standalone reconciler attached"}))

    async def post_control(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            action = body.get("action", "")
            if not action:
                return secure_response(JSONResponse({"status": "error", "message": "Missing action parameter"}, status_code=400))

            # Security Action PIN Verification
            if auth_mgr and auth_mgr.enabled and auth_mgr.pin_hash:
                pin = str(body.get("pin", "")).strip()
                if not pin:
                    return secure_response(JSONResponse({"status": "error", "message": "Security PIN required for bot control"}, status_code=403))
                if not auth_mgr.verify_pin(pin):
                    return secure_response(JSONResponse({"status": "error", "message": "Invalid Security PIN"}, status_code=403))

            state.request_control(action)
            return secure_response(JSONResponse({"status": "ok", "action": action}))
        except Exception as e:
            return secure_response(JSONResponse({"status": "error", "message": str(e)}, status_code=400))

    async def get_multi_market(request: Request) -> Response:
        auth_err = check_auth(request)
        if auth_err:
            return auth_err
        mm_state = getattr(state, "multi_market", None) or {}
        return secure_response(JSONResponse(mm_state))

    routes = [
        Route("/", endpoint=get_index, methods=["GET"]),
        Route("/dashboard", endpoint=get_index, methods=["GET"]),
        Route("/login", endpoint=get_login, methods=["GET"]),
        Route("/api/auth/login", endpoint=post_auth_login, methods=["POST"]),
        Route("/api/auth/logout", endpoint=post_auth_logout, methods=["POST"]),
        Route("/api/auth/me", endpoint=get_auth_me, methods=["GET"]),
        Route("/api/state", endpoint=get_state, methods=["GET"]),
        Route("/api/multi_market", endpoint=get_multi_market, methods=["GET"]),
        Route("/api/performance", endpoint=get_performance, methods=["GET"]),
        Route("/api/trades", endpoint=get_trades, methods=["GET"]),
        Route("/api/logs", endpoint=get_logs, methods=["GET"]),
        Route("/api/trades/csv", endpoint=export_trades_csv, methods=["GET"]),
        Route("/api/capital", endpoint=get_capital, methods=["GET"]),
        Route("/api/capital/transactions", endpoint=get_capital_transactions, methods=["GET"]),
        Route("/api/capital/reconciliations", endpoint=get_capital_reconciliations, methods=["GET"]),
        Route("/api/capital/reconcile", endpoint=post_capital_reconcile, methods=["POST"]),
        Route("/api/control", endpoint=post_control, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    return Starlette(debug=False, routes=routes)


class DashboardServer:
    """Manages the background Uvicorn server in a dedicated thread."""

    def __init__(
        self,
        state: DashboardState,
        ledger: TradeLedger,
        capital_mgr: Optional[Any] = None,
        reconciler: Optional[Any] = None,
        auth_mgr: Optional[AuthManager] = None,
        host: str = "127.0.0.1",
        port: int = 8080,
    ):
        import threading
        self.state = state
        self.ledger = ledger
        self.capital_mgr = capital_mgr
        self.reconciler = reconciler
        self.auth_mgr = auth_mgr
        self.host = host
        self.port = port
        self.app = create_dashboard_app(
            state,
            ledger,
            capital_mgr=capital_mgr,
            reconciler=reconciler,
            auth_mgr=auth_mgr,
        )
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def _run_server(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    async def start(self) -> None:
        self.start_sync()

    def start_sync(self) -> None:
        import threading
        import time
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_server, daemon=True, name="DashboardServerThread")
        self._thread.start()
        time.sleep(0.15)
        log.info("Live Dashboard workstation running at http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        self.stop_sync()

    def stop_sync(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        log.info("Dashboard workstation server stopped.")
