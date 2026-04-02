"""Tests for alert system fixes: placeholder detection, channel validation,
startup status logging.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from src.reporting.alert_system import AlertConfig, AlertSystem


# ────────────────────────────────────────────────────────────────────
# Fix 3: Placeholder detection
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("your_telegram_bot_token_here", True),
        ("your-discord-webhook", True),
        ("changeme", True),
        ("placeholder", True),
        ("xxx", True),
        ("none", True),
        ("todo", True),
        ("<token>", True),
        ("${TELEGRAM_TOKEN}", True),
        ("", True),
        ("   ", True),
        ("7123456789:AAH_real_looking_token", False),
        ("https://discord.com/api/webhooks/123/abc", False),
    ],
)
def test_is_placeholder(value: str, expected: bool) -> None:
    assert AlertSystem._is_placeholder(value) is expected


def test_telegram_rejects_placeholder_token() -> None:
    config = AlertConfig(
        telegram_bot_token="your_telegram_bot_token_here",
        telegram_chat_id="123456",
    )
    alert = AlertSystem(config=config)
    assert alert._telegram_configured() is False


def test_telegram_rejects_empty_chat_id() -> None:
    config = AlertConfig(
        telegram_bot_token="7123456789:AAH_real_token",
        telegram_chat_id="",
    )
    alert = AlertSystem(config=config)
    assert alert._telegram_configured() is False


def test_telegram_accepts_valid_config() -> None:
    config = AlertConfig(
        telegram_bot_token="7123456789:AAH_real_token",
        telegram_chat_id="123456",
    )
    alert = AlertSystem(config=config)
    assert alert._telegram_configured() is True


def test_discord_rejects_placeholder_url() -> None:
    config = AlertConfig(discord_webhook_url="your_discord_webhook_url")
    alert = AlertSystem(config=config)
    assert alert._discord_configured() is False


def test_discord_rejects_non_https() -> None:
    config = AlertConfig(discord_webhook_url="http://discord.com/api/webhooks/123/abc")
    alert = AlertSystem(config=config)
    assert alert._discord_configured() is False


def test_discord_accepts_valid_https_url() -> None:
    config = AlertConfig(
        discord_webhook_url="https://discord.com/api/webhooks/123/abc"
    )
    alert = AlertSystem(config=config)
    assert alert._discord_configured() is True


def test_log_channel_status_logs_off(caplog: pytest.LogCaptureFixture) -> None:
    config = AlertConfig()  # All defaults = empty = off
    alert = AlertSystem(config=config)
    with caplog.at_level(logging.INFO):
        alert.log_channel_status()
    assert "Telegram=OFF" in caplog.text
    assert "Discord=OFF" in caplog.text
    assert "Console=ON" in caplog.text
    assert "console-only" in caplog.text


def test_log_channel_status_logs_on(caplog: pytest.LogCaptureFixture) -> None:
    config = AlertConfig(
        telegram_bot_token="7123456789:AAH_real",
        telegram_chat_id="123",
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    alert = AlertSystem(config=config)
    with caplog.at_level(logging.INFO):
        alert.log_channel_status()
    assert "Telegram=ON" in caplog.text
    assert "Discord=ON" in caplog.text
    assert "console-only" not in caplog.text
