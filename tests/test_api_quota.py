from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from api_quota_notify import maybe_notify_klickanalytics_quota_reached
from alerts import build_api_quota_discord_embed
from data import DataError, KlickAnalyticsQuotaError, is_klickanalytics_monthly_limit_message


@pytest.mark.parametrize(
    "message",
    [
        "Error: Monthly API limit reached for your plan.",
        "monthly quota exceeded",
        "You have reached your monthly usage limit.",
        "KlickAnalytics CLI command failed: monthly request limit exhausted",
        "monthly_cli_limit_reached",
        '{"error_code": "monthly_cli_limit_reached", "stderr": "Monthly CLI usage limit reached (500)\\n"}',
    ],
)
def test_monthly_limit_detection(message: str):
    assert is_klickanalytics_monthly_limit_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "ConnectionError: cannot reach KlickAnalytics endpoint.",
        "KlickAnalytics CLI timed out",
        "Missing required columns",
        "daily rate limit exceeded",
    ],
)
def test_non_monthly_errors_not_detected(message: str):
    assert not is_klickanalytics_monthly_limit_message(message)


def test_run_ka_json_raises_quota_error(monkeypatch):
    import data

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "Monthly API limit reached for this account."

    monkeypatch.setattr(data.subprocess, "run", lambda *args, **kwargs: Proc())
    with pytest.raises(KlickAnalyticsQuotaError):
        data._run_ka_json("ka", ["prices", "-s", "QQQ"], "test-key")


def test_run_ka_json_raises_generic_data_error(monkeypatch):
    import data

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "invalid symbol"

    monkeypatch.setattr(data.subprocess, "run", lambda *args, **kwargs: Proc())
    with pytest.raises(DataError):
        data._run_ka_json("ka", ["prices", "-s", "QQQ"], "test-key")


def test_quota_notify_once_per_month(tmp_path: Path):
    state_path = tmp_path / "api_quota_notified.json"
    logger = __import__("logging").getLogger("test")

    with patch("api_quota_notify.send_discord_api_quota_alert") as send_mock:
        assert maybe_notify_klickanalytics_quota_reached(
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            state_path=state_path,
            detail="monthly limit reached",
            logger=logger,
        )
        assert send_mock.call_count == 1
        assert not maybe_notify_klickanalytics_quota_reached(
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            state_path=state_path,
            detail="monthly limit reached",
            logger=logger,
        )
        assert send_mock.call_count == 1

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "notified_month" in saved


def test_api_quota_embed_pretty_fields():
    detail = (
        'KlickAnalytics CLI command failed: HTTPError: 429\n'
        '{"ok": false, "error_code": "monthly_cli_limit_reached", '
        '"stderr": "Monthly CLI usage limit reached (500)\\n", '
        '"data": {"monthly_limit": 500, "total_hits": 500}}'
    )
    embed = build_api_quota_discord_embed(detail, "2026-06-25T14:00:00Z")
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "CLI usage this month" in names
    assert "What you can do" in names
    usage = next(f["value"] for f in embed["fields"] if f["name"] == "CLI usage this month")  # type: ignore[index]
    assert "500" in usage
    assert "100%" in usage
    assert "Data feed paused" in embed["description"]  # type: ignore[operator]
