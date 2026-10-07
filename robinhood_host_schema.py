"""Confirmed Robinhood MCP place/review schema adapter (Phase 5R.1).

Official equities tools (Robinhood support, Trading with your agent, 2026-10-07):
``review_equity_order``, ``place_equity_order``, ``cancel_equity_order``; order
types include market (share/dollar), limit, stop. Input JSON schemas are not
published. This module therefore:

- Allowlists only documented / conservative argument keys
- Never invents ``client_order_id`` (unpublished as a place param)
- Uses marketable LIMIT when limit capability is confirmed; otherwise FAIL CLOSED
  for real-money (no silent market fallback)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from brokers.types import QuoteView, TradeIntent, UnsafeBrokerConfiguration

# Confirmed from official docs — not from guessing tools/list JSON.
DOCS_CONFIRMED_LIMIT_ORDERS = True
DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM = False

REQUIRED_WRITE_TOOLS = frozenset({"place_equity_order", "review_equity_order"})
OPTIONAL_WRITE_TOOLS = frozenset({"cancel_equity_order"})

# Keys we may send on place/review. Anything else is stripped / rejected.
PLACE_ARG_ALLOWLIST = frozenset(
    {
        "symbol",
        "side",
        "order_type",
        "quantity",
        "limit_price",
        "time_in_force",
        "sell_all",
        # Dollar market exists in docs; pilot prefers share+limit and never uses
        # dollar market for real-money without an explicit future confirmation.
    }
)

# Explicitly forbidden even if present on an intent / draft.
PLACE_ARG_FORBIDDEN = frozenset(
    {
        "client_order_id",
        "clientOrderId",
        "ref_id",
        "idempotency_key",
        "oauth_token",
        "access_token",
        "refresh_token",
        "authorization",
        "bearer",
    }
)


@dataclass(frozen=True)
class HostMcpCapabilities:
    """Capability snapshot from tools/list and/or docs confirmation."""

    place_equity_order: bool
    review_equity_order: bool
    cancel_equity_order: bool
    limit_orders: bool
    client_order_id_param: bool
    source: str

    def missing_required(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.place_equity_order:
            missing.append("place_equity_order")
        if not self.review_equity_order:
            missing.append("review_equity_order")
        if not self.limit_orders:
            missing.append("limit_orders")
        return tuple(missing)


def docs_confirmed_capabilities() -> HostMcpCapabilities:
    """Baseline capabilities from official public docs (no tools/list yet)."""
    return HostMcpCapabilities(
        place_equity_order=True,
        review_equity_order=True,
        cancel_equity_order=True,
        limit_orders=DOCS_CONFIRMED_LIMIT_ORDERS,
        client_order_id_param=DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM,
        source="official_docs_2026-10-07",
    )


def capabilities_from_tools_list(tools: Any) -> HostMcpCapabilities:
    """Parse an MCP tools/list payload into capabilities; fail closed on gaps."""
    names: set[str] = set()
    limit_hint = DOCS_CONFIRMED_LIMIT_ORDERS
    client_id_hint = DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM
    rows: list[Any]
    if isinstance(tools, dict):
        rows = list(tools.get("tools") or tools.get("result", {}).get("tools") or [])
    elif isinstance(tools, list):
        rows = list(tools)
    else:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("tool") or "").strip()
        if name:
            names.add(name)
        if name == "place_equity_order":
            schema = row.get("inputSchema") or row.get("input_schema") or row.get("parameters") or {}
            props = {}
            if isinstance(schema, dict):
                props = schema.get("properties") or schema.get("fields") or {}
            if isinstance(props, dict):
                keys = {str(k) for k in props}
                if "limit_price" in keys or "order_type" in keys:
                    limit_hint = True
                if "client_order_id" in keys or "clientOrderId" in keys:
                    client_id_hint = True
    if not names:
        return HostMcpCapabilities(
            place_equity_order=False,
            review_equity_order=False,
            cancel_equity_order=False,
            limit_orders=False,
            client_order_id_param=False,
            source="empty_tools_list",
        )
    return HostMcpCapabilities(
        place_equity_order="place_equity_order" in names,
        review_equity_order="review_equity_order" in names,
        cancel_equity_order="cancel_equity_order" in names,
        limit_orders=limit_hint,
        client_order_id_param=client_id_hint,
        source="tools/list",
    )


def assert_capabilities_ready(caps: HostMcpCapabilities) -> None:
    missing = caps.missing_required()
    if missing:
        raise UnsafeBrokerConfiguration(
            "Robinhood host MCP capabilities missing required tools/features: "
            + ", ".join(missing)
            + "; refusing real-money place (fail closed)"
        )


def _money(price: float) -> str:
    return f"{round(float(price), 2):.2f}"


def marketable_limit_price(
    *,
    action: str,
    quote: QuoteView | None,
    slippage_bps: float,
) -> str:
    """BUY at ask*(1+slip); SELL at bid*(1-slip). Fail closed without a quote."""
    side = (action or "").strip().upper()
    if quote is None:
        raise UnsafeBrokerConfiguration("marketable limit requires a fresh quote")
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    if side == "BUY":
        if quote.ask is None or float(quote.ask) <= 0:
            raise UnsafeBrokerConfiguration("buy limit requires a positive ask")
        return _money(float(quote.ask) * (1.0 + slip))
    if side == "SELL":
        if quote.bid is None or float(quote.bid) <= 0:
            raise UnsafeBrokerConfiguration("sell limit requires a positive bid")
        return _money(float(quote.bid) * (1.0 - slip))
    raise UnsafeBrokerConfiguration(f"unsupported action {action!r}")


def filter_place_args(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip forbidden + non-allowlisted keys. Fail if a forbidden key was present."""
    forbidden_hit = sorted(k for k in raw if k in PLACE_ARG_FORBIDDEN)
    if forbidden_hit:
        raise UnsafeBrokerConfiguration(
            "refusing undocumented/forbidden place args: " + ", ".join(forbidden_hit)
        )
    return {k: v for k, v in raw.items() if k in PLACE_ARG_ALLOWLIST}


def build_place_args(
    intent: TradeIntent,
    *,
    quote: QuoteView | None,
    capabilities: HostMcpCapabilities,
    slippage_bps: float,
) -> dict[str, Any]:
    """Build allowlisted place/review args. Never includes client_order_id."""
    assert_capabilities_ready(capabilities)
    if capabilities.client_order_id_param:
        # Even if tools/list someday shows it, pilot still keeps internal id only
        # until a dedicated confirmation + reconcile design lands. Fail closed
        # rather than silently start sending an unverified field shape.
        raise UnsafeBrokerConfiguration(
            "client_order_id appeared in tools/list; refusing until explicit "
            "pilot confirmation (internal UNIQUE id + reconcile remains authority)"
        )
    if not capabilities.limit_orders:
        raise UnsafeBrokerConfiguration(
            "limit orders unsupported on this host MCP; refusing real-money "
            "(no silent market fallback)"
        )

    side = "buy" if intent.action.upper() == "BUY" else "sell"
    limit = marketable_limit_price(
        action=intent.action,
        quote=quote,
        slippage_bps=slippage_bps,
    )
    args: dict[str, Any] = {
        "symbol": intent.execution_symbol.upper(),
        "side": side,
        "order_type": "limit",
        "time_in_force": "gfd",
        "limit_price": limit,
    }
    if side == "sell":
        if intent.quantity <= 0:
            args["sell_all"] = True
        else:
            args["quantity"] = int(intent.quantity)
    else:
        if intent.quantity < 1:
            raise UnsafeBrokerConfiguration("buy quantity must be a positive whole share count")
        args["quantity"] = int(intent.quantity)
    return filter_place_args(args)


__all__ = [
    "DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM",
    "DOCS_CONFIRMED_LIMIT_ORDERS",
    "HostMcpCapabilities",
    "PLACE_ARG_ALLOWLIST",
    "PLACE_ARG_FORBIDDEN",
    "REQUIRED_WRITE_TOOLS",
    "assert_capabilities_ready",
    "build_place_args",
    "capabilities_from_tools_list",
    "docs_confirmed_capabilities",
    "filter_place_args",
    "marketable_limit_price",
]
