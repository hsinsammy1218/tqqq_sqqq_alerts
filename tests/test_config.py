from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import ConfigError, load_settings
from main import _missing_live_webhook_message


def test_load_settings_rejects_unknown_position_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "klickanalytics")
    monkeypatch.setenv("KLICKANALYTICS_CLI_API_KEY", "test-key")
    monkeypatch.setenv("POSITION_STATE_BACKEND", "s3")
    with pytest.raises(ConfigError, match="POSITION_STATE_BACKEND must be"):
        load_settings()


def test_load_settings_supabase_requires_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "klickanalytics")
    monkeypatch.setenv("KLICKANALYTICS_CLI_API_KEY", "test-key")
    monkeypatch.setenv("POSITION_STATE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    with pytest.raises(ConfigError, match="requires SUPABASE_URL"):
        load_settings()


def test_missing_live_webhook_message():
    assert (
        _missing_live_webhook_message(SimpleNamespace(dry_run=False, discord_webhook_url=""))
        == "DISCORD_WEBHOOK_URL is required when not in dry-run mode."
    )
    assert _missing_live_webhook_message(SimpleNamespace(dry_run=True, discord_webhook_url="")) is None
    assert (
        _missing_live_webhook_message(
            SimpleNamespace(dry_run=False, discord_webhook_url="https://discord.example/hook")
        )
        is None
    )
