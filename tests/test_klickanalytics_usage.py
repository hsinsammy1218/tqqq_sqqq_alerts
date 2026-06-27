from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from klickanalytics_usage import (
    maybe_notify_klickanalytics_usage_warning,
    record_monthly_cli_usage,
    track_and_maybe_warn_cli_usage,
)
from klickanalytics_usage import MonthlyUsageSnapshot


def test_record_monthly_cli_usage_accumulates(tmp_path: Path):
    usage_path = tmp_path / "usage.json"
    first = record_monthly_cli_usage(2, monthly_limit=500, warn_pct=80, usage_path=usage_path)
    assert first is not None
    assert first.total_calls == 2
    second = record_monthly_cli_usage(2, monthly_limit=500, warn_pct=80, usage_path=usage_path)
    assert second is not None
    assert second.total_calls == 4


def test_record_syncs_server_total(tmp_path: Path):
    usage_path = tmp_path / "usage.json"
    record_monthly_cli_usage(2, monthly_limit=500, warn_pct=80, usage_path=usage_path)
    synced = record_monthly_cli_usage(
        1,
        monthly_limit=500,
        warn_pct=80,
        usage_path=usage_path,
        server_total=450,
    )
    assert synced is not None
    assert synced.total_calls == 450


def test_usage_warning_sent_once_per_month(tmp_path: Path):
    usage_path = tmp_path / "usage.json"
    warn_path = tmp_path / "warn.json"
    logger = __import__("logging").getLogger("test")
    snapshot = MonthlyUsageSnapshot(
        month="2026-06",
        total_calls=410,
        monthly_limit=500,
        warn_threshold=400,
        remaining=90,
    )

    with patch("klickanalytics_usage.send_discord_api_usage_warning") as send_mock:
        assert maybe_notify_klickanalytics_usage_warning(
            snapshot,
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            warn_pct=80,
            logger=logger,
            warn_notified_path=warn_path,
        )
        assert send_mock.call_count == 1
        assert not maybe_notify_klickanalytics_usage_warning(
            snapshot,
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            warn_pct=80,
            logger=logger,
            warn_notified_path=warn_path,
        )
        assert send_mock.call_count == 1

    saved = json.loads(warn_path.read_text(encoding="utf-8"))
    assert saved["notified_month"] == "2026-06"


def test_track_and_warn_skips_at_limit(tmp_path: Path, monkeypatch):
    usage_path = tmp_path / "usage.json"
    warn_path = tmp_path / "warn.json"
    logger = __import__("logging").getLogger("test")
    monkeypatch.setattr("klickanalytics_usage.USAGE_PATH", usage_path)
    monkeypatch.setattr("klickanalytics_usage.WARN_NOTIFIED_PATH", warn_path)

    with patch("klickanalytics_usage.send_discord_api_usage_warning") as send_mock:
        snapshot = track_and_maybe_warn_cli_usage(
            calls=2,
            webhook_url="https://discord.test/webhook",
            dry_run=False,
            monthly_limit=500,
            warn_pct=80,
            logger=logger,
            quota_error_detail=(
                '{"error_code":"monthly_cli_limit_reached","data":{"monthly_limit":500,"total_hits":500}}'
            ),
        )
        assert snapshot is not None
        assert snapshot.total_calls >= 500
        assert send_mock.call_count == 0
