"""
Monitoring, heartbeat tracking, and optional Telegram alerting service for polybot.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger("polybot.monitor")


class TelegramAlerter:
    """Sends high-priority trading alerts to Telegram with graceful failure suppression."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.enabled = bool(self.bot_token and self.chat_id)

        if self.enabled:
            log.info("Telegram alerting enabled for chat %s", self.chat_id)
        else:
            log.debug("Telegram alerting disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing)")

    def send_message(self, text: str) -> bool:
        """Sends a text message via Telegram Bot API."""
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            log.warning("Telegram notification failed: %s", e)
            return False

    def send_trade_alert(
        self,
        slug: str,
        side: str,
        price: float,
        shares: float,
        cost_usd: float,
        strategy: str,
        edge: float = 0.0,
    ) -> bool:
        msg = (
            f"🚀 *TRADE ENTERED*\n"
            f"• Market: `{slug}`\n"
            f"• Side: *{side}*\n"
            f"• Price: `{price:.3f}`\n"
            f"• Shares: `{shares:.2f}`\n"
            f"• Cost: `${cost_usd:.2f}`\n"
            f"• Strategy: `{strategy}` (Edge: `{edge:+.3f}`)"
        )
        return self.send_message(msg)

    def send_settlement_alert(
        self,
        slug: str,
        side: str,
        winner: str,
        payout_usd: float,
        pnl_usd: float,
        total_pnl_usd: float,
    ) -> bool:
        emoji = "✅" if winner == side else "❌"
        msg = (
            f"{emoji} *POSITION SETTLED*\n"
            f"• Market: `{slug}`\n"
            f"• Position: *{side}* | Winner: *{winner}*\n"
            f"• Payout: `${payout_usd:.2f}`\n"
            f"• Net PnL: *${pnl_usd:+.2f}*\n"
            f"• Cumulative PnL: *${total_pnl_usd:+.2f}*"
        )
        return self.send_message(msg)

    def send_risk_alert(self, reason: str) -> bool:
        msg = f"⚠️ *RISK CIRCUIT BREAKER TRIGGERED*\n• Reason: `{reason}`"
        return self.send_message(msg)

    def send_crash_alert(self, error_message: str) -> bool:
        msg = f"🚨 *CRITICAL BOT ALERT*\n• Error: `{error_message}`"
        return self.send_message(msg)

    def send_daily_summary(
        self,
        total_trades: int,
        wins: int,
        win_rate: float,
        daily_pnl: float,
    ) -> bool:
        msg = (
            f"📊 *DAILY SUMMARY REPORT*\n"
            f"• Total Trades: `{total_trades}`\n"
            f"• Wins: `{wins}`\n"
            f"• Win Rate: `{win_rate:.1f}%`\n"
            f"• Daily PnL: *${daily_pnl:+.2f}*"
        )
        return self.send_message(msg)
