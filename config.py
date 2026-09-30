from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from alpaca_paper import PAPER_TRADING_BASE_URL, is_live_trading_host
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    qqq_ticker: str
    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_data_base_url: str
    alpaca_data_feed: str
    alpaca_paper_trading: bool
    alpaca_trading_base_url: str
    alpaca_paper_notional: float
    alpaca_paper_equity_pct: float
    alpaca_paper_limit_offset_bps: int
    alpaca_paper_vol_sizing: bool
    alpaca_paper_risk_fraction: float
    alpaca_paper_vol_stop: str
    alpaca_paper_atr_stop_mult: float
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
    trade_log_jsonl: Path
    trade_log_backend: str
    trade_log_bot_id: str
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
    exit_mode: str
    atr_trail_mult: float
    backtest_entry_slippage_bps: float
    backtest_exit_slippage_bps: float
    alpaca_monthly_limit: int
    alpaca_usage_warn_pct: int


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
            exit_mode=settings.exit_mode,
            atr_trail_mult=settings.atr_trail_mult,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_settings() -> Settings:
    load_dotenv()

    qqq_ticker = os.getenv("QQQ_TICKER", "QQQ").strip().upper()
    alpaca_api_key = os.getenv("ALPACA_API_KEY", "").strip()
    alpaca_api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
    alpaca_data_base_url = (
        os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").strip()
        or "https://data.alpaca.markets"
    )
    alpaca_data_feed = (os.getenv("ALPACA_DATA_FEED", "iex").strip() or "iex").lower()
    alpaca_paper_trading = _to_bool(os.getenv("ALPACA_PAPER_TRADING", "false"), default=False)
    alpaca_trading_base_url = (
        os.getenv("ALPACA_TRADING_BASE_URL", PAPER_TRADING_BASE_URL).strip() or PAPER_TRADING_BASE_URL
    ).rstrip("/")
    if is_live_trading_host(alpaca_trading_base_url):
        raise ConfigError(
            "Live Alpaca trading host is not enabled. "
            f"Use {PAPER_TRADING_BASE_URL} only (ALPACA_TRADING_BASE_URL)."
        )
    alpaca_paper_notional = float(os.getenv("ALPACA_PAPER_NOTIONAL", "500"))
    alpaca_paper_equity_pct = float(os.getenv("ALPACA_PAPER_EQUITY_PCT", "0"))
    alpaca_paper_limit_offset_bps = int(os.getenv("ALPACA_PAPER_LIMIT_OFFSET_BPS", "10"))
    if alpaca_paper_notional < 0:
        raise ConfigError("ALPACA_PAPER_NOTIONAL must be >= 0.")
    if alpaca_paper_equity_pct < 0:
        raise ConfigError("ALPACA_PAPER_EQUITY_PCT must be >= 0.")
    if alpaca_paper_limit_offset_bps < 0:
        raise ConfigError("ALPACA_PAPER_LIMIT_OFFSET_BPS must be >= 0.")
    alpaca_paper_vol_sizing = _to_bool(os.getenv("ALPACA_PAPER_VOL_SIZING", "false"), default=False)
    alpaca_paper_risk_fraction = float(os.getenv("ALPACA_PAPER_RISK_FRACTION", "0.0075"))
    if alpaca_paper_risk_fraction <= 0 or alpaca_paper_risk_fraction > 0.02:
        raise ConfigError("ALPACA_PAPER_RISK_FRACTION must be in (0, 0.02].")
    alpaca_paper_vol_stop = (os.getenv("ALPACA_PAPER_VOL_STOP", "stop_pct").strip().lower() or "stop_pct")
    if alpaca_paper_vol_stop not in {"stop_pct", "atr"}:
        raise ConfigError("ALPACA_PAPER_VOL_STOP must be 'stop_pct' or 'atr'.")
    alpaca_paper_atr_stop_mult = float(os.getenv("ALPACA_PAPER_ATR_STOP_MULT", "2"))
    if alpaca_paper_atr_stop_mult <= 0:
        raise ConfigError("ALPACA_PAPER_ATR_STOP_MULT must be positive.")
    exit_mode = (os.getenv("EXIT_MODE", "fixed").strip().lower() or "fixed")
    if exit_mode not in {"fixed", "atr_trail"}:
        raise ConfigError("EXIT_MODE must be 'fixed' or 'atr_trail'.")
    atr_trail_mult = float(os.getenv("ATR_TRAIL_MULT", "2"))
    if atr_trail_mult <= 0:
        raise ConfigError("ATR_TRAIL_MULT must be positive.")
    backtest_entry_slippage_bps = float(os.getenv("BACKTEST_ENTRY_SLIPPAGE_BPS", "10"))
    backtest_exit_slippage_bps = float(os.getenv("BACKTEST_EXIT_SLIPPAGE_BPS", "10"))
    if backtest_entry_slippage_bps < 0 or backtest_exit_slippage_bps < 0:
        raise ConfigError("BACKTEST_*_SLIPPAGE_BPS must be >= 0.")
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    dry_run = _to_bool(os.getenv("DRY_RUN", "true"), default=True)

    if not alpaca_api_key or not alpaca_api_secret:
        raise ConfigError("ALPACA_API_KEY and ALPACA_API_SECRET are required.")

    position_state_backend = os.getenv("POSITION_STATE_BACKEND", "file").strip().lower() or "file"
    if position_state_backend not in {"file", "supabase"}:
        raise ConfigError("POSITION_STATE_BACKEND must be 'file' or 'supabase'.")
    position_state_bot_id = os.getenv("POSITION_STATE_BOT_ID", "default").strip() or "default"
    # Dedicated trade-log backend; default file locally. Render Blueprint sets supabase.
    # When unset, mirror POSITION_STATE_BACKEND so one supabase latch covers both.
    trade_log_backend_raw = os.getenv("TRADE_LOG_BACKEND")
    if trade_log_backend_raw is None or not str(trade_log_backend_raw).strip():
        trade_log_backend = position_state_backend
    else:
        trade_log_backend = str(trade_log_backend_raw).strip().lower()
    if trade_log_backend not in {"file", "supabase"}:
        raise ConfigError("TRADE_LOG_BACKEND must be 'file' or 'supabase'.")
    trade_log_bot_id = (
        os.getenv("TRADE_LOG_BOT_ID") or position_state_bot_id or "default"
    ).strip() or "default"
    if position_state_backend == "supabase" or trade_log_backend == "supabase":
        supabase_url = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
        supabase_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not supabase_url or not supabase_key:
            which = (
                "POSITION_STATE_BACKEND=supabase"
                if position_state_backend == "supabase"
                else "TRADE_LOG_BACKEND=supabase"
            )
            raise ConfigError(
                f"{which} requires SUPABASE_URL "
                "(or NEXT_PUBLIC_SUPABASE_URL) and SUPABASE_SERVICE_ROLE_KEY."
            )

    return Settings(
        qqq_ticker=qqq_ticker,
        alpaca_api_key=alpaca_api_key,
        alpaca_api_secret=alpaca_api_secret,
        alpaca_data_base_url=alpaca_data_base_url.rstrip("/"),
        alpaca_data_feed=alpaca_data_feed,
        alpaca_paper_trading=alpaca_paper_trading,
        alpaca_trading_base_url=alpaca_trading_base_url,
        alpaca_paper_notional=alpaca_paper_notional,
        alpaca_paper_equity_pct=alpaca_paper_equity_pct,
        alpaca_paper_limit_offset_bps=alpaca_paper_limit_offset_bps,
        alpaca_paper_vol_sizing=alpaca_paper_vol_sizing,
        alpaca_paper_risk_fraction=alpaca_paper_risk_fraction,
        alpaca_paper_vol_stop=alpaca_paper_vol_stop,
        alpaca_paper_atr_stop_mult=alpaca_paper_atr_stop_mult,
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
        trade_log_jsonl=Path(os.getenv("TRADE_LOG_JSONL", "logs/trades.jsonl")),
        trade_log_backend=trade_log_backend,
        trade_log_bot_id=trade_log_bot_id,
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
        exit_mode=exit_mode,
        atr_trail_mult=atr_trail_mult,
        backtest_entry_slippage_bps=backtest_entry_slippage_bps,
        backtest_exit_slippage_bps=backtest_exit_slippage_bps,
        alpaca_monthly_limit=int(os.getenv("ALPACA_MONTHLY_LIMIT", "0")),
        alpaca_usage_warn_pct=int(os.getenv("ALPACA_USAGE_WARN_PCT", "80")),
    )
