"""Connected shadow: real Robinhood reads, simulated intent, no submission.

FLIP never treats the exit leg as filled. The second leg stays blocked.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import requests

from brokers.mode import ROBINHOOD_CONNECTED_SHADOW, execution_broker_from_environ
from brokers.robinhood_normalize import normalize_snapshot
from brokers.robinhood_reader import (
    RobinhoodReadClient,
    RobinhoodReadError,
    RobinhoodToolRejected,
    transport_from_env,
)
from brokers.types import EXECUTION_NOT_SUBMITTED, BrokerState
from robinhood_audit import ShadowAuditLog, redact
from robinhood_reconcile import reconcile_books
from robinhood_risk import limits_from_env
from robinhood_shadow import ShadowRun, run_robinhood_shadow
from strategy_types import AlertDecision, PositionState


def _position_from_broker(state: BrokerState, local: PositionState | None) -> PositionState | None:
    """Use the single real ETF holding so a flip can propose that sell."""
    held = [
        row.symbol.upper()
        for row in state.positions
        if row.symbol.upper() in {"TQQQ", "SQQQ"} and row.qty > 0
    ]
    unique = sorted(set(held))
    if len(unique) == 1:
        return PositionState(active_symbol=unique[0])
    return local


def _connected_text(text: str) -> str:
    return (
        text.replace("ROBINHOOD SHADOW TRADE", "ROBINHOOD CONNECTED SHADOW")
        .replace("SHADOW MODE", "CONNECTED SHADOW")
        .replace("ROBINHOOD SHADOW", "ROBINHOOD CONNECTED SHADOW")
    )


def _unknown(detail: str, *, data_bar_start: datetime | None, now: datetime) -> BrokerState:
    return BrokerState(
        known=False,
        detail=detail,
        data_bar_start=data_bar_start,
        now=now,
        order_history_complete=False,
    )


def load_connected_state(
    transport,
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> tuple[BrokerState, RobinhoodReadClient | None]:
    """Read and normalize. Any failure returns an unknown state and no submission."""
    if transport is None:
        return (
            _unknown(
                "Robinhood reader is not configured",
                data_bar_start=data_bar_start,
                now=now,
            ),
            None,
        )
    client = RobinhoodReadClient(transport)
    try:
        raw = client.read_snapshot()
        state = normalize_snapshot(raw, now=now, data_bar_start=data_bar_start)
    except (RobinhoodReadError, RobinhoodToolRejected, ValueError, TypeError, KeyError):
        return (
            _unknown(
                "Robinhood read failed",
                data_bar_start=data_bar_start,
                now=now,
            ),
            client,
        )
    return state, client


def run_connected_shadow(
    alert: AlertDecision,
    position_before: PositionState | None,
    state: BrokerState,
    limits,
    *,
    broker=None,
) -> tuple[ShadowRun, str]:
    """Plan a connected-shadow decision. There is no exit-fill argument to pass."""
    book = reconcile_books(position_before, state)
    buy_block = book.reason if book.blocks_new_exposure else None
    intent_position = _position_from_broker(state, position_before) if state.known else position_before
    result = run_robinhood_shadow(
        alert,
        intent_position,
        state,
        limits,
        broker=broker,
        buy_block_reason=buy_block,
    )
    text = _connected_text(result.text)
    if any(leg.execution_status != EXECUTION_NOT_SUBMITTED for leg in result.legs):
        raise RuntimeError("connected shadow produced a status other than NOT_SUBMITTED")
    if result.submission_attempts != 0:
        raise RuntimeError("connected shadow attempted a broker submission")
    return replace(result, text=text), ("unknown" if not state.known else "mismatch" if buy_block else "known")


def _post_discord(webhook_url: str, text: str, *, dry_run: bool) -> None:
    payload = redact(
        {
            "username": "QQQ Swing Alerts",
            "content": "ROBINHOOD CONNECTED SHADOW — NO REAL MONEY TRADED",
            "embeds": [
                {
                    "title": "Robinhood connected shadow",
                    "description": text[:3900],
                    "color": 0x6B7280,
                }
            ],
        }
    )
    if dry_run or not webhook_url:
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if getattr(response, "status_code", 0) >= 400:
            print(f"[robinhood-connected] Discord failed: {response.status_code}")
    except requests.RequestException:
        print("[robinhood-connected] Discord error")


def run_connected_shadow_after_strategy(
    alert: AlertDecision,
    position_before: PositionState | None,
    *,
    dry_run: bool,
    webhook_url: str,
    data_bar_start: datetime | None,
    now: datetime | None,
    env: Mapping[str, str] | None = None,
    transport=None,
) -> ShadowRun | None:
    """Hook used by main. Reads only when a transport exists. Never submits."""
    source = os.environ if env is None else env
    if execution_broker_from_environ(source) != ROBINHOOD_CONNECTED_SHADOW:
        return None
    clock = now or datetime.now(timezone.utc)
    reader = transport if transport is not None else transport_from_env(source)
    state, _client = load_connected_state(reader, now=clock, data_bar_start=data_bar_start)
    limits = limits_from_env(source)
    log_path = Path(source.get("ROBINHOOD_SHADOW_LOG", "logs/robinhood_shadow.jsonl"))
    audit = ShadowAuditLog(log_path)
    known_ids = set(state.known_client_order_ids)
    for row in audit.read():
        client_id = row.get("client_order_id")
        if isinstance(client_id, str) and client_id:
            known_ids.add(client_id)
    state = replace(state, known_client_order_ids=frozenset(known_ids))
    result, broker_state = run_connected_shadow(alert, position_before, state, limits)
    for leg in result.legs:
        audit.append(leg.audit_row(broker_state=broker_state, execution_mode="connected_shadow"))
    _post_discord(webhook_url, result.text, dry_run=dry_run)
    print(result.text)
    return result
