"""Env-gated paper-path risk controls (kill switch, buy cap, TQQQ-only).

Defaults are OFF / no-op so current Render paper soak is unchanged.
When enabled, these are safe rehearsal for a later capped live (A) path.
They never lift the live-host block and never touch Robinhood.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from data import alpaca_auth_headers
from runtime_logging import log_event


class PaperRiskError(Exception):
    """Non-fatal risk-control failure (logged; does not abort the alert run)."""


@dataclass(frozen=True)
class PaperRiskLimits:
    """Paper-path ops caps. Zero / false means that control is off."""

    max_buy_notional: float = 0.0
    tqqq_only: bool = False
    max_daily_loss_usd: float = 0.0
    max_weekly_loss_usd: float = 0.0

    def any_enabled(self) -> bool:
        return bool(
            (self.max_buy_notional and self.max_buy_notional > 0)
            or self.tqqq_only
            or (self.max_daily_loss_usd and self.max_daily_loss_usd > 0)
            or (self.max_weekly_loss_usd and self.max_weekly_loss_usd > 0)
        )


@dataclass(frozen=True)
class KillStatus:
    tripped: bool
    reason: str
    equity: float | None = None
    daily_pnl: float | None = None
    weekly_pnl: float | None = None
    last_equity: float | None = None
    week_start_equity: float | None = None


def apply_buy_notional_cap(notional: float, max_buy_notional: float) -> float:
    """Clamp buy notional when ``max_buy_notional`` > 0; otherwise unchanged."""
    if max_buy_notional and max_buy_notional > 0:
        return min(float(notional), float(max_buy_notional))
    return float(notional)


def is_buy_side(side: str) -> bool:
    return (side or "").strip().lower() == "buy"


def filter_intents_for_tqqq_only(
    intents: list[Any],
    *,
    tqqq_only: bool,
) -> tuple[list[Any], list[str]]:
    """Drop SQQQ buy legs when TQQQ-only is on. Sells (flatten) still pass.

    Returns (kept_intents, skip_details).
    """
    if not tqqq_only:
        return list(intents), []
    kept: list[Any] = []
    skipped: list[str] = []
    for intent in intents:
        symbol = str(getattr(intent, "symbol", "") or "").upper()
        side = str(getattr(intent, "side", "") or "").lower()
        if is_buy_side(side) and symbol == "SQQQ":
            skipped.append(
                f"PAPER_TQQQ_ONLY=true blocked buy {symbol} ({getattr(intent, 'purpose', '')})"
            )
            continue
        kept.append(intent)
    return kept, skipped


def filter_intents_for_kill(
    intents: list[Any],
    *,
    kill: KillStatus,
) -> tuple[list[Any], list[str]]:
    """When kill is tripped, allow sells only (flatten); block new buys."""
    if not kill.tripped:
        return list(intents), []
    kept: list[Any] = []
    skipped: list[str] = []
    for intent in intents:
        side = str(getattr(intent, "side", "") or "").lower()
        if is_buy_side(side):
            skipped.append(
                f"KILL blocked buy {getattr(intent, 'symbol', '')} "
                f"({getattr(intent, 'purpose', '')}): {kill.reason}"
            )
            continue
        kept.append(intent)
    return kept, skipped


def evaluate_loss_kill(
    *,
    equity: float | None,
    last_equity: float | None,
    week_start_equity: float | None,
    max_daily_loss_usd: float,
    max_weekly_loss_usd: float,
) -> KillStatus:
    """Trip when realized+unrealized sleeve loss vs day/week mark exceeds caps.

    Daily PnL uses Alpaca ``last_equity`` (prior session close) vs current equity.
    Weekly PnL uses portfolio-history week-start equity vs current.
    Caps of 0 disable that check. Missing marks fail open (no trip) with no reason.
    """
    daily_pnl: float | None = None
    weekly_pnl: float | None = None

    if equity is not None and last_equity is not None:
        daily_pnl = float(equity) - float(last_equity)
    if equity is not None and week_start_equity is not None:
        weekly_pnl = float(equity) - float(week_start_equity)

    if max_daily_loss_usd and max_daily_loss_usd > 0 and daily_pnl is not None:
        if daily_pnl <= -float(max_daily_loss_usd):
            return KillStatus(
                tripped=True,
                reason=(
                    f"daily loss {daily_pnl:.2f} USD breached "
                    f"PAPER_MAX_DAILY_LOSS_USD={max_daily_loss_usd:g}"
                ),
                equity=equity,
                daily_pnl=daily_pnl,
                weekly_pnl=weekly_pnl,
                last_equity=last_equity,
                week_start_equity=week_start_equity,
            )

    if max_weekly_loss_usd and max_weekly_loss_usd > 0 and weekly_pnl is not None:
        if weekly_pnl <= -float(max_weekly_loss_usd):
            return KillStatus(
                tripped=True,
                reason=(
                    f"weekly loss {weekly_pnl:.2f} USD breached "
                    f"PAPER_MAX_WEEKLY_LOSS_USD={max_weekly_loss_usd:g}"
                ),
                equity=equity,
                daily_pnl=daily_pnl,
                weekly_pnl=weekly_pnl,
                last_equity=last_equity,
                week_start_equity=week_start_equity,
            )

    return KillStatus(
        tripped=False,
        reason="",
        equity=equity,
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        last_equity=last_equity,
        week_start_equity=week_start_equity,
    )


def fetch_paper_account(
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Return the Alpaca paper account payload (equity, last_equity, …)."""
    url = urljoin(trading_base_url.rstrip("/") + "/", "v2/account")
    client = session or requests
    try:
        response = client.get(
            url,
            headers=alpaca_auth_headers(api_key, api_secret),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PaperRiskError(f"Alpaca account fetch failed: {exc}") from exc
    body = (response.text or "").strip()
    if response.status_code >= 400:
        raise PaperRiskError(
            f"Alpaca account error ({response.status_code}): {body or response.reason}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaperRiskError("Alpaca account returned non-JSON.") from exc
    if not isinstance(payload, dict):
        raise PaperRiskError("Unexpected Alpaca account payload.")
    return payload


def parse_equity_fields(account: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (equity, last_equity) from an account payload; None when unparsable."""

    def _num(key: str) -> float | None:
        raw = account.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    equity = _num("equity")
    if equity is None:
        equity = _num("cash")
    last_equity = _num("last_equity")
    return equity, last_equity


def fetch_week_start_equity(
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    session: requests.Session | None = None,
) -> float | None:
    """Best-effort week-start equity from Alpaca portfolio history (1W / 1D)."""
    url = urljoin(trading_base_url.rstrip("/") + "/", "v2/account/portfolio/history")
    client = session or requests
    try:
        response = client.get(
            url,
            headers=alpaca_auth_headers(api_key, api_secret),
            params={"period": "1W", "timeframe": "1D"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PaperRiskError(f"Alpaca portfolio history failed: {exc}") from exc
    if response.status_code >= 400:
        body = (response.text or "").strip()
        raise PaperRiskError(
            f"Alpaca portfolio history error ({response.status_code}): {body or response.reason}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaperRiskError("Alpaca portfolio history returned non-JSON.") from exc
    if not isinstance(payload, dict):
        raise PaperRiskError("Unexpected portfolio history payload.")
    equities = payload.get("equity")
    if not isinstance(equities, list) or not equities:
        return None
    for raw in equities:
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def assess_kill_from_broker(
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    limits: PaperRiskLimits,
    session: requests.Session | None = None,
    logger: Any | None = None,
) -> KillStatus:
    """Fetch account marks and evaluate daily/weekly kill. Fail-open on API errors."""
    need_daily = bool(limits.max_daily_loss_usd and limits.max_daily_loss_usd > 0)
    need_weekly = bool(limits.max_weekly_loss_usd and limits.max_weekly_loss_usd > 0)
    if not need_daily and not need_weekly:
        return KillStatus(tripped=False, reason="")

    equity: float | None = None
    last_equity: float | None = None
    week_start: float | None = None

    try:
        account = fetch_paper_account(
            api_key=api_key,
            api_secret=api_secret,
            trading_base_url=trading_base_url,
            session=session,
        )
        equity, last_equity = parse_equity_fields(account)
    except PaperRiskError as exc:
        if logger is not None:
            log_event(logger, 30, "Paper risk account fetch failed", error=str(exc))
        print(f"[paper-risk] Account fetch failed (kill check skipped): {exc}")
        return KillStatus(tripped=False, reason="")

    if need_weekly:
        try:
            week_start = fetch_week_start_equity(
                api_key=api_key,
                api_secret=api_secret,
                trading_base_url=trading_base_url,
                session=session,
            )
        except PaperRiskError as exc:
            if logger is not None:
                log_event(logger, 30, "Paper risk week equity fetch failed", error=str(exc))
            print(f"[paper-risk] Week equity fetch failed (weekly kill skipped): {exc}")

    return evaluate_loss_kill(
        equity=equity,
        last_equity=last_equity,
        week_start_equity=week_start,
        max_daily_loss_usd=limits.max_daily_loss_usd,
        max_weekly_loss_usd=limits.max_weekly_loss_usd,
    )
