from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from api_quota_notify import maybe_notify_klickanalytics_quota_reached
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
