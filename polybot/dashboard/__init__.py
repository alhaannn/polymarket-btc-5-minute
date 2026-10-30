"""
Local web dashboard module for polybot.
"""
from polybot.dashboard.server import DashboardServer, create_dashboard_app
from polybot.dashboard.state import DashboardState

__all__ = ["DashboardState", "DashboardServer", "create_dashboard_app"]
