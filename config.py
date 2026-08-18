from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams


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
    position_state_backend: str
    position_state_bot_id: str
    events_json: Path
    event_risk_avoidance: bool
    event_risk_calendar_url: str
    anchor_date: str
    score_weights_csv: str
    regime_sep_atr_mult: float
    regime_slope_atr_mult: float
    regime_ranging_threshold_weight_add: float
    regime_trend_favorable_delta: float
    flip_min_hold_trading_days: int
    flip_margin_weight: float
    entry_dominance_gap_weight: float
    flip_in_range_regime: bool
    min_confidence_to_trade: int
    klickanalytics_monthly_limit: int
    klickanalytics_usage_warn_pct: int


def _to_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_score_weights(value: str) -> tuple[float, ...]:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_SCORE_WEIGHTS
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 8:
        raise ConfigError("SCORE_WEIGHTS must contain exactly 8 comma-separated non-negative numbers.")
    try:
        out = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise ConfigError("SCORE_WEIGHTS must be numeric.") from exc
    if any(w < 0 for w in out):
        raise ConfigError("SCORE_WEIGHTS values must be non-negative.")
    return out


def strategy_params_from_settings(settings: Settings) -> StrategyParams:
    try:
        return StrategyParams(
            bull_entry_threshold=settings.bull_entry_threshold,
            bear_entry_threshold=settings.bear_entry_threshold,
            weak_threshold=settings.weak_score_threshold,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            stretch_take_profit_pct=settings.stretch_take_profit_pct,
            max_hold_days=settings.max_hold_trading_days,
            entry_atr_multiplier=settings.entry_atr_multiplier,
            score_weights=_parse_score_weights(settings.score_weights_csv),
            regime_sep_atr_mult=settings.regime_sep_atr_mult,
            regime_slope_atr_mult=settings.regime_slope_atr_mult,
            regime_ranging_threshold_weight_add=settings.regime_ranging_threshold_weight_add,
            regime_trend_favorable_delta=settings.regime_trend_favorable_delta,
            flip_min_hold_trading_days=settings.flip_min_hold_trading_days,
            flip_margin_weight=settings.flip_margin_weight,
            entry_dominance_gap_weight=settings.entry_dominance_gap_weight,
            flip_in_range_regime=settings.flip_in_range_regime,
            min_confidence_to_trade=settings.min_confidence_to_trade,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_settings() -> Settings:
    load_dotenv()

    qqq_ticker = os.getenv("QQQ_TICKER", "QQQ").strip().upper()
    klickanalytics_api_key = os.getenv("KLICKANALYTICS_CLI_API_KEY", "").strip()
    klickanalytics_cli_command = os.getenv("KLICKANALYTICS_CLI_COMMAND", "ka").strip()
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    dry_run = _to_bool(os.getenv("DRY_RUN", "true"), default=True)

    if not klickanalytics_api_key:
        raise ConfigError("KLICKANALYTICS_CLI_API_KEY is required.")

    position_state_backend = os.getenv("POSITION_STATE_BACKEND", "file").strip().lower() or "file"
    if position_state_backend not in {"file", "supabase"}:
        raise ConfigError("POSITION_STATE_BACKEND must be 'file' or 'supabase'.")
    position_state_bot_id = os.getenv("POSITION_STATE_BOT_ID", "default").strip() or "default"
    if position_state_backend == "supabase":
        supabase_url = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
        supabase_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not supabase_url or not supabase_key:
            raise ConfigError(
                "POSITION_STATE_BACKEND=supabase requires SUPABASE_URL "
                "(or NEXT_PUBLIC_SUPABASE_URL) and SUPABASE_SERVICE_ROLE_KEY."
            )

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
        position_state_backend=position_state_backend,
        position_state_bot_id=position_state_bot_id,
        events_json=Path(os.getenv("EVENTS_JSON", "events.json")),
        event_risk_avoidance=_to_bool(os.getenv("EVENT_RISK_AVOIDANCE", "false"), default=False),
        event_risk_calendar_url=os.getenv("EVENT_RISK_CALENDAR_URL", "").strip(),
        anchor_date=os.getenv("ANCHOR_DATE", "").strip(),
        score_weights_csv=os.getenv("SCORE_WEIGHTS", "").strip(),
        regime_sep_atr_mult=float(os.getenv("REGIME_SEP_ATR_MULT", "0.12")),
        regime_slope_atr_mult=float(os.getenv("REGIME_SLOPE_ATR_MULT", "0.03")),
        regime_ranging_threshold_weight_add=float(os.getenv("REGIME_RANGING_THRESHOLD_WEIGHT_ADD", "0.55")),
        regime_trend_favorable_delta=float(os.getenv("REGIME_TREND_FAVORABLE_DELTA", "0.5")),
        flip_min_hold_trading_days=int(os.getenv("FLIP_MIN_HOLD_TRADING_DAYS", "3")),
        flip_margin_weight=float(os.getenv("FLIP_MARGIN_WEIGHT", "1.25")),
        entry_dominance_gap_weight=float(os.getenv("ENTRY_DOMINANCE_GAP_WEIGHT", "1.25")),
        flip_in_range_regime=_to_bool(os.getenv("FLIP_ALLOW_IN_RANGE", "false"), default=False),
        # Locked from walk-forward rank 1 (reports/walk_forward_results.csv): avg_score best row → 62.
        min_confidence_to_trade=int(os.getenv("MIN_CONFIDENCE_TO_TRADE", "62")),
        klickanalytics_monthly_limit=int(os.getenv("KLICKANALYTICS_MONTHLY_LIMIT", "500")),
        klickanalytics_usage_warn_pct=int(os.getenv("KLICKANALYTICS_USAGE_WARN_PCT", "80")),
    )
