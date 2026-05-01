from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    qqq_ticker: str
    klickanalytics_api_key: str
    klickanalytics_cli_command: str
    discord_webhook_url: str
    dry_run: bool
    bull_entry_threshold: int
    bear_entry_threshold: int
    weak_score_threshold: int
    stop_loss_pct: float
    take_profit_pct: float
    stretch_take_profit_pct: float
    max_hold_trading_days: int
    entry_atr_multiplier: float
    journal_csv: Path
    position_state_json: Path
    events_json: Path
    event_risk_avoidance: bool
    event_risk_calendar_url: str
    anchor_date: str


def _to_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    qqq_ticker = os.getenv("QQQ_TICKER", "QQQ").strip().upper()
    klickanalytics_api_key = os.getenv("KLICKANALYTICS_CLI_API_KEY", "").strip()
    klickanalytics_cli_command = os.getenv("KLICKANALYTICS_CLI_COMMAND", "ka").strip()
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    dry_run = _to_bool(os.getenv("DRY_RUN", "true"), default=True)

    if not klickanalytics_api_key:
        raise ConfigError("KLICKANALYTICS_CLI_API_KEY is required.")

    return Settings(
        qqq_ticker=qqq_ticker,
        klickanalytics_api_key=klickanalytics_api_key,
        klickanalytics_cli_command=klickanalytics_cli_command,
        discord_webhook_url=webhook,
        dry_run=dry_run,
        bull_entry_threshold=int(os.getenv("BULL_ENTRY_THRESHOLD", "5")),
        bear_entry_threshold=int(os.getenv("BEAR_ENTRY_THRESHOLD", "5")),
        weak_score_threshold=int(os.getenv("WEAK_SCORE_THRESHOLD", "3")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.08")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.15")),
        stretch_take_profit_pct=float(os.getenv("STRETCH_TAKE_PROFIT_PCT", "0.25")),
        max_hold_trading_days=int(os.getenv("MAX_HOLD_TRADING_DAYS", "10")),
        entry_atr_multiplier=float(os.getenv("ENTRY_ATR_MULTIPLIER", "0.5")),
        journal_csv=Path(os.getenv("JOURNAL_CSV", "alerts_journal.csv")),
        position_state_json=Path(os.getenv("POSITION_STATE_JSON", "position_state.json")),
        events_json=Path(os.getenv("EVENTS_JSON", "events.json")),
        event_risk_avoidance=_to_bool(os.getenv("EVENT_RISK_AVOIDANCE", "false"), default=False),
        event_risk_calendar_url=os.getenv("EVENT_RISK_CALENDAR_URL", "").strip(),
        anchor_date=os.getenv("ANCHOR_DATE", "").strip(),
    )
