"""Robinhood agentic playbook for TQQQ/SQQQ alerts.

This module never calls Robinhood and never places an order. It turns a
strategy alert into the official Trading MCP steps an external agent should
run on a dedicated agentic account:

https://agent.robinhood.com/mcp/trading

Default is review-only. ``place_equity_order`` is withheld unless the caller
sets ``allow_place=True`` (this process does not).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from strategy_types import AlertDecision, PositionState

RH_MCP_URL = "https://agent.robinhood.com/mcp/trading"
_TRADEABLE = frozenset({"TQQQ", "SQQQ"})


@dataclass(frozen=True)
class _Leg:
    symbol: str
    side: str
    purpose: str


def _legs(alert: AlertDecision, position_before: PositionState | None) -> list[_Leg]:
    alert_type = (alert.alert_type or "").upper()
    symbol = (alert.symbol or "").upper()
    if alert_type == "BUY" and symbol in _TRADEABLE:
        return [_Leg(symbol, "buy", "entry")]
    if alert_type == "SELL" and symbol in _TRADEABLE:
        return [_Leg(symbol, "sell", "exit")]
    if alert_type == "FLIP" and symbol in _TRADEABLE:
        legs: list[_Leg] = []
        held = ""
        if position_before and position_before.active_symbol:
            held = position_before.active_symbol.upper()
        if held in _TRADEABLE and held != symbol:
            legs.append(_Leg(held, "sell", "flip_exit"))
        legs.append(_Leg(symbol, "buy", "flip_entry"))
        return legs
    return []


def _review_args(leg: _Leg, notional_usd: float) -> dict[str, Any]:
    """Argument shape aimed at review_equity_order.

    Confirm against a live ``tools/list`` schema before the first real call.
    Dollar-based so the notional cap is the size, not a share guess.
    """
    return {
        "symbol": leg.symbol,
        "side": leg.side,
        "order_type": "market",
        "notional_usd": round(float(notional_usd), 2),
        "time_in_force": "gfd",
    }


def build_rh_playbook(
    alert: AlertDecision,
    position_before: PositionState | None = None,
    *,
    max_notional_usd: float = 200.0,
    tqqq_only: bool = True,
    allow_place: bool = False,
) -> dict[str, Any]:
    """Build a review-first MCP playbook. Does not perform I/O."""
    if max_notional_usd <= 0:
        raise ValueError("max_notional_usd must be positive.")
    notional = float(max_notional_usd)
    legs = _legs(alert, position_before)
    blocked: list[str] = []
    kept: list[_Leg] = []
    for leg in legs:
        if tqqq_only and leg.side == "buy" and leg.symbol == "SQQQ":
            blocked.append(f"TQQQ-only: skipped {leg.purpose} buy SQQQ")
            continue
        kept.append(leg)

    preflight = [
        {"tool": "get_accounts", "arguments": {}, "writes": False, "purpose": "confirm agentic account"},
        {"tool": "get_portfolio", "arguments": {}, "writes": False, "purpose": "buying power"},
        {"tool": "get_equity_positions", "arguments": {}, "writes": False, "purpose": "reconcile holdings"},
        {
            "tool": "get_trade_approval_setting",
            "arguments": {},
            "writes": False,
            "purpose": "Trade approvals should stay ON until you explicitly allow unattended orders",
        },
    ]

    if not kept:
        status = "no_order" if not blocked else "blocked"
        reason = blocked[0] if blocked else "No position — nothing for the agent to trade."
        if (alert.alert_type or "").upper() not in {"BUY", "SELL", "FLIP"} and not blocked:
            reason = "No position — alert is not a buy, sell, or flip."
        return {
            "mcp_url": RH_MCP_URL,
            "status": status,
            "reason": reason,
            "allow_place": False,
            "alert_type": alert.alert_type,
            "symbol": alert.symbol,
            "max_notional_usd": notional,
            "tqqq_only": tqqq_only,
            "blocked": blocked,
            "steps": preflight,
            "withheld_place_steps": [],
            "instruction": (
                "Do not call place_equity_order. Read positions, then stop. "
                "Internal decision id may still be CASH."
            ),
        }

    steps: list[dict[str, Any]] = list(preflight)
    withheld: list[dict[str, Any]] = []
    for leg in kept:
        args = _review_args(leg, notional if leg.side == "buy" else notional)
        # Sells should flatten the agentic holding; notional is a cap hint.
        if leg.side == "sell":
            args = {
                "symbol": leg.symbol,
                "side": "sell",
                "order_type": "market",
                "sell_all": True,
            }
        review = {
            "tool": "review_equity_order",
            "arguments": args,
            "writes": False,
            "purpose": leg.purpose,
        }
        steps.append(review)
        place = {
            "tool": "place_equity_order",
            "arguments": dict(args),
            "writes": True,
            "purpose": leg.purpose,
            "only_after": "review_equity_order",
        }
        if allow_place:
            steps.append(place)
        else:
            withheld.append(place)

    return {
        "mcp_url": RH_MCP_URL,
        "status": "ready_to_review",
        "reason": "Review these orders in the agentic account. This bot does not place them.",
        "allow_place": bool(allow_place),
        "alert_type": alert.alert_type,
        "symbol": alert.symbol,
        "max_notional_usd": notional,
        "tqqq_only": tqqq_only,
        "blocked": blocked,
        "steps": steps,
        "withheld_place_steps": withheld,
        "instruction": (
            "Call review_equity_order only. Leave place_equity_order withheld "
            "unless Trade approvals are ON and you have separately allowed placement. "
            "Use the dedicated Robinhood agentic account, not the main brokerage account."
            if not allow_place
            else "Place only after a clean review_equity_order on the agentic account."
        ),
    }


def format_rh_playbook(playbook: dict[str, Any]) -> str:
    return json.dumps(playbook, indent=2)
