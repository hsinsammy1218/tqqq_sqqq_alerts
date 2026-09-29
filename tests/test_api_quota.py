from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from api_quota_notify import maybe_notify_quota_reached
from alerts import build_api_quota_discord_embed
from data import is_rate_limit_message


@pytest.mark.parametrize(
    "message",
    [
        "Error: Monthly API limit reached for your plan.",
        "monthly quota exceeded",
        "rate limit exceeded",
        "too many requests",
        "Alpaca market data QQQ 1Day failed (429): rate limit",
    ],
)
def test_rate_limit_detection(message: str):
    assert is_rate_limit_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "ConnectionError: cannot reach Alpaca endpoint.",
        "Alpaca market data timed out",
        "Missing required columns",
    ],
)
def test_non_rate_limit_errors_not_detected(message: str):
    assert not is_rate_limit_message(message)


def test_quota_notify_once_per_month(tmp_path: Path):
    state_path = tmp_path / "api_quota_notified.json"
    logger = __import__("logging").getLogger("test")

    with patch("api_quota_notify.send_discord_api_quota_alert") as send_mock:
        assert maybe_notify_quota_reached(
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            state_path=state_path,
            detail="rate limit reached",
            logger=logger,
        )
        assert send_mock.call_count == 1
        assert not maybe_notify_quota_reached(
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            state_path=state_path,
            detail="rate limit reached",
            logger=logger,
        )
        assert send_mock.call_count == 1

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "notified_month" in saved


def test_api_quota_embed_pretty_fields():
    detail = (
        'Alpaca market data QQQ 1Day failed (429): '
        '{"message": "rate limit exceeded", '
        '"data": {"monthly_limit": 500, "total_hits": 500}}'
    )
    embed = build_api_quota_discord_embed(detail, "2026-06-25T14:00:00Z")
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "API usage this month" in names or "Status" in names
    assert "What you can do" in names
    assert "Data feed paused" in embed["description"]  # type: ignore[operator]
    assert "Alpaca" in embed["title"]  # type: ignore[operator]
