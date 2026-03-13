"""
Multi-channel alert system for Claude Quant.

Supports Telegram (via Bot API), Discord (via webhook), and console logging.
All channels degrade gracefully — a failed notification never crashes the
trading loop.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.reporting.alert_system")

# Maximum message length for Telegram (4096 chars) and Discord (2000 chars).
_TELEGRAM_MAX_LEN = 4096
_DISCORD_MAX_LEN = 2000


# ---------------------------------------------------------------------------
# Enums and models
# ---------------------------------------------------------------------------


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertConfig(BaseModel):
    """Configuration for the alert system, typically loaded from env vars."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    console_enabled: bool = True

    @classmethod
    def from_env(cls) -> AlertConfig:
        """Build config from environment variables."""
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            console_enabled=True,
        )


# ---------------------------------------------------------------------------
# AlertSystem
# ---------------------------------------------------------------------------


class AlertSystem:
    """Multi-channel notification system.

    Parameters
    ----------
    config : AlertConfig | None
        If ``None``, configuration is loaded from environment variables.
    daily_loss_alert_pct : Decimal
        If the daily loss exceeds this percentage, a critical alert is
        automatically sent.  Default 5 %.
    """

    def __init__(
        self,
        config: AlertConfig | None = None,
        daily_loss_alert_pct: Decimal = Decimal("5"),
    ) -> None:
        self._config = config or AlertConfig.from_env()
        self._daily_loss_alert_pct = daily_loss_alert_pct
        self._http_client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=15.0)
        return self._http_client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> AlertSystem:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- public API ----------------------------------------------------------

    async def send_alert(
        self,
        message: str,
        level: AlertLevel | str = AlertLevel.INFO,
    ) -> None:
        """Send a text alert to all configured channels.

        Parameters
        ----------
        message : str
            The alert body.
        level : AlertLevel | str
            Severity level.  ``"critical"`` messages are always sent to
            all channels; ``"info"`` may be console-only depending on config.
        """
        if isinstance(level, str):
            level = AlertLevel(level.lower())

        prefix = self._level_prefix(level)
        full_message = f"{prefix} {message}"
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        timestamped = f"[{now}] {full_message}"

        # Console — always
        if self._config.console_enabled:
            self._log_to_console(timestamped, level)

        # Telegram
        if self._telegram_configured():
            await self._send_telegram(timestamped)

        # Discord
        if self._discord_configured():
            await self._send_discord(timestamped)

    async def send_daily_report(self, report: str) -> None:
        """Send a daily Markdown report to all channels.

        Long reports are truncated for Telegram/Discord and the full
        report is logged to file.
        """
        header = "DAILY REPORT"
        await self._send_to_all(header, report)

    async def send_trade_notification(self, trade: dict[str, Any]) -> None:
        """Send a notification when a trade is opened or closed.

        Parameters
        ----------
        trade : dict
            Must include: ``symbol``, ``direction``, ``entry_price``,
            and optionally ``exit_price``, ``pnl``, ``strategy``.
        """
        symbol = trade.get("symbol", "?")
        direction = str(trade.get("direction", "?")).upper()
        entry = trade.get("entry_price", "?")
        exit_price = trade.get("exit_price")
        pnl = trade.get("pnl")
        strategy = trade.get("strategy", "")
        leverage = trade.get("leverage", "")

        if exit_price is not None:
            # Trade closed
            pnl_str = f"{Decimal(str(pnl)):+}" if pnl is not None else "N/A"
            msg = (
                f"TRADE CLOSED | {symbol} {direction} | "
                f"Entry: {entry} | Exit: {exit_price} | "
                f"PnL: {pnl_str} USDT | {strategy}"
            )
            level = AlertLevel.INFO if pnl and Decimal(str(pnl)) >= 0 else AlertLevel.WARNING
        else:
            # Trade opened
            sl = trade.get("stop_loss", "?")
            tp = trade.get("take_profit", "?")
            msg = (
                f"TRADE OPENED | {symbol} {direction} | "
                f"Entry: {entry} | SL: {sl} | TP: {tp} | "
                f"Leverage: {leverage}x | {strategy}"
            )
            level = AlertLevel.INFO

        await self.send_alert(msg, level)

    async def send_circuit_breaker_alert(
        self,
        old_level: str,
        new_level: str,
    ) -> None:
        """Alert when the circuit breaker level changes.

        A transition to RED or DEAD is always CRITICAL.
        """
        if new_level.upper() in ("RED", "DEAD"):
            level = AlertLevel.CRITICAL
        elif new_level.upper() == "YELLOW":
            level = AlertLevel.WARNING
        else:
            level = AlertLevel.INFO

        msg = (
            f"CIRCUIT BREAKER CHANGE: {old_level.upper()} -> {new_level.upper()}"
        )

        if new_level.upper() == "DEAD":
            msg += " | ALL TRADING HALTED — manual intervention required"
        elif new_level.upper() == "RED":
            msg += " | Survival mode: 1 position max, 3x leverage"
        elif new_level.upper() == "YELLOW":
            msg += " | Reduced risk: 50% sizes, 5x leverage max"

        await self.send_alert(msg, level)

    async def check_daily_loss(
        self,
        start_balance: Decimal,
        current_balance: Decimal,
    ) -> None:
        """Auto-send a critical alert if the daily loss exceeds the threshold.

        Parameters
        ----------
        start_balance : Decimal
            Balance at the start of the UTC day.
        current_balance : Decimal
            Current balance.
        """
        if start_balance <= Decimal("0"):
            return

        loss_pct = ((start_balance - current_balance) / start_balance) * Decimal("100")

        if loss_pct >= self._daily_loss_alert_pct:
            await self.send_alert(
                f"DAILY LOSS ALERT: Balance down {loss_pct:.2f}% "
                f"(from {start_balance} to {current_balance} USDT). "
                f"Threshold: {self._daily_loss_alert_pct}%.",
                AlertLevel.CRITICAL,
            )

    # -- channel checks ------------------------------------------------------

    def _telegram_configured(self) -> bool:
        return bool(self._config.telegram_bot_token and self._config.telegram_chat_id)

    def _discord_configured(self) -> bool:
        return bool(self._config.discord_webhook_url)

    # -- Telegram ------------------------------------------------------------

    async def _send_telegram(self, message: str) -> None:
        """Send *message* via Telegram Bot API.  Silently fails on error."""
        try:
            client = await self._get_client()
            # Truncate if needed
            text = message[:_TELEGRAM_MAX_LEN]
            url = (
                f"https://api.telegram.org/bot{self._config.telegram_bot_token}"
                f"/sendMessage"
            )
            payload = {
                "chat_id": self._config.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            resp = await client.post(url, json=payload)

            if resp.status_code != 200:
                logger.warning(
                    "Telegram API returned %d: %s", resp.status_code, resp.text[:200]
                )
            else:
                logger.debug("Telegram message sent (%d chars)", len(text))
        except Exception as exc:
            logger.warning("Failed to send Telegram message: %s", exc)

    # -- Discord -------------------------------------------------------------

    async def _send_discord(self, message: str) -> None:
        """Send *message* via Discord webhook.  Silently fails on error."""
        try:
            client = await self._get_client()
            # Truncate if needed
            text = message[:_DISCORD_MAX_LEN]
            payload = {"content": text}
            resp = await client.post(
                self._config.discord_webhook_url, json=payload
            )

            if resp.status_code not in (200, 204):
                logger.warning(
                    "Discord webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                logger.debug("Discord message sent (%d chars)", len(text))
        except Exception as exc:
            logger.warning("Failed to send Discord message: %s", exc)

    # -- Console -------------------------------------------------------------

    @staticmethod
    def _log_to_console(message: str, level: AlertLevel) -> None:
        """Log to Python logger at the appropriate level."""
        if level == AlertLevel.CRITICAL:
            logger.critical(message)
        elif level == AlertLevel.WARNING:
            logger.warning(message)
        else:
            logger.info(message)

    # -- helpers -------------------------------------------------------------

    async def _send_to_all(self, header: str, body: str) -> None:
        """Send a long-form message to all channels, truncating as needed."""
        full = f"--- {header} ---\n{body}"

        if self._config.console_enabled:
            logger.info(full)

        if self._telegram_configured():
            # For long reports, send a summary header + truncated body
            tg_msg = full[:_TELEGRAM_MAX_LEN]
            await self._send_telegram(tg_msg)

        if self._discord_configured():
            dc_msg = full[:_DISCORD_MAX_LEN]
            await self._send_discord(dc_msg)

    @staticmethod
    def _level_prefix(level: AlertLevel) -> str:
        return {
            AlertLevel.INFO: "[INFO]",
            AlertLevel.WARNING: "[WARNING]",
            AlertLevel.CRITICAL: "[CRITICAL]",
        }.get(level, "[INFO]")
