"""
Local Web Dashboard Server powered by Starlette and Uvicorn.
Provides real-time REST endpoints and serves the light-themed financial workstation UI.
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
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from polybot.dashboard.state import DashboardState
from polybot.execution.ledger import TradeLedger

log = logging.getLogger("polybot.dashboard.server")

STATIC_DIR = Path(__file__).parent / "static"


def create_dashboard_app(state: DashboardState, ledger: TradeLedger) -> Starlette:
    """Builds Starlette application for the trading dashboard."""

    async def get_index(request: Request) -> Response:
        index_file = STATIC_DIR / "index.html"
        return FileResponse(index_file)

    async def get_state(request: Request) -> JSONResponse:
        return JSONResponse(state.to_dict())

    async def get_performance(request: Request) -> JSONResponse:
        summary = ledger.get_performance_summary()
        return JSONResponse(summary)

    async def get_trades(request: Request) -> JSONResponse:
        trades = ledger.get_recent_trades(limit=100)
        return JSONResponse(trades)

    async def export_trades_csv(request: Request) -> Response:
        trades = ledger.get_recent_trades(limit=1000)
        output = io.StringIO()
        if trades:
            writer = csv.DictWriter(output, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
        else:
            output.write("order_id,slug,status,pnl_usd\n")

        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=paper_trades.csv"},
        )

    async def post_control(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            action = body.get("action", "")
            if action:
                state.request_control(action)
                return JSONResponse({"status": "ok", "action": action})
            return JSONResponse({"status": "error", "message": "Missing action parameter"}, status_code=400)
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

    routes = [
        Route("/", endpoint=get_index, methods=["GET"]),
        Route("/dashboard", endpoint=get_index, methods=["GET"]),
        Route("/api/state", endpoint=get_state, methods=["GET"]),
        Route("/api/performance", endpoint=get_performance, methods=["GET"]),
        Route("/api/trades", endpoint=get_trades, methods=["GET"]),
        Route("/api/trades/csv", endpoint=export_trades_csv, methods=["GET"]),
        Route("/api/control", endpoint=post_control, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    return Starlette(debug=False, routes=routes)


class DashboardServer:
    """Manages the background Uvicorn server task."""

    def __init__(
        self,
        state: DashboardState,
        ledger: TradeLedger,
        host: str = "127.0.0.1",
        port: int = 8080,
    ):
        self.state = state
        self.ledger = ledger
        self.host = host
        self.port = port
        self.app = create_dashboard_app(state, ledger)
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        log.info("Live Dashboard workstation running at http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Dashboard workstation server stopped.")
