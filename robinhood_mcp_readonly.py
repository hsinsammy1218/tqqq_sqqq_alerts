"""Phase 5R.2 — authenticated Robinhood MCP **read-only** preflight helpers.

Official endpoint only: ``https://agent.robinhood.com/mcp/trading``.

This module does **not** open sockets or perform OAuth. It classifies tools from
a ``tools/list`` (or docs fixture) payload, enforces a fail-closed allowlist, and
provides sanitized mock schemas for offline tests.

Absolute refusals for this phase:
- ``place_*`` / ``review_*`` / ``cancel_*`` / stage / replace
- Any UNKNOWN tool (treated as mutating)
- Arming live submission or inventing unattended auth
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from brokers.robinhood_reader import ALLOWED_READS, AUTH_CASE, UNATTENDED_AUTH_SUPPORTED

OFFICIAL_MCP_URL = "https://agent.robinhood.com/mcp/trading"

# Phase 5R.2 may invoke these AFTER interactive OAuth on an approved host.
# Narrower than the full public docs catalog — matches connected-shadow reads
# plus QQQ quote coverage and optional trade-approval status.
PREFLIGHT_READ_ALLOWLIST = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
        "get_trade_approval_setting",
        "get_equity_tradability",
        "get_indexes",
        "get_index_quotes",
    }
)

# Explicit mutating / order / mutation surface from Robinhood public docs
# ("Trading with your agent", 2026-10-07). Never invoke in Phase 5R.2.
PREFLIGHT_MUTATING_DENYLIST = frozenset(
    {
        # Equities
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        # Options
        "review_option_order",
        "place_option_order",
        "cancel_option_order",
        "exercise_option",
        "cancel_option_exercise",
        # Crypto
        "preview_crypto_order",
        "place_crypto_order",
        "cancel_crypto_order",
        # Advanced
        "review_advanced_order",
        "place_advanced_order",
        "cancel_advanced_order",
        # Trade approvals (decline is a write)
        "decline_trade_approval",
        # Watchlists
        "create_watchlist",
        "update_watchlist",
        "follow_watchlist",
        "unfollow_watchlist",
        "add_to_watchlist",
        "remove_from_watchlist",
        "add_option_to_watchlist",
        "remove_option_from_watchlist",
        # Alerts
        "create_alert",
        "update_alert",
        "delete_alert",
        "mark_alerts_read",
        # Scanner writes
        "create_scan",
        "update_scan_filters",
        "update_scan_config",
        # Legend writes
        "set_legend_chart_settings",
        "add_legend_indicator",
        "update_legend_indicator",
        "remove_legend_indicator",
    }
)

# Name patterns that are always mutating even if absent from the denylist.
_MUTATING_NAME_RE = re.compile(
    r"^(place_|cancel_|review_|preview_|create_|update_|delete_|add_|remove_|"
    r"set_|follow_|unfollow_|mark_|exercise_|decline_)",
    re.IGNORECASE,
)

# Account / order identifiers to redact in reports (never log raw tokens).
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|authorization|bearer|refresh|access_token|"
    r"client_secret|api_key|account_number|account_id|order_id|id)$",
    re.IGNORECASE,
)


class ToolClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ClassifiedTool:
    name: str
    classification: ToolClass
    description: str
    property_keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AllowlistDecision:
    allowed: bool
    tool: str
    classification: ToolClass
    reason: str


def classify_tool_name(name: str) -> ClassifiedTool:
    """Classify a single tool name fail-closed."""
    tool = (name or "").strip()
    if not tool:
        return ClassifiedTool("", ToolClass.UNKNOWN, "", (), "empty tool name")
    if tool in PREFLIGHT_MUTATING_DENYLIST or _MUTATING_NAME_RE.match(tool):
        return ClassifiedTool(
            tool,
            ToolClass.MUTATING,
            "",
            (),
            "denylist or mutating name prefix",
        )
    if tool in PREFLIGHT_READ_ALLOWLIST:
        return ClassifiedTool(
            tool,
            ToolClass.READ_ONLY,
            "",
            (),
            "preflight read allowlist",
        )
    # Docs-confirmed get_* that are not on the narrow preflight list stay
    # UNKNOWN → refused (fail closed) until an operator expands the allowlist.
    if tool.startswith("get_") or tool in {"search", "run_scan", "preview_scan"}:
        return ClassifiedTool(
            tool,
            ToolClass.UNKNOWN,
            "",
            (),
            "documented-looking read outside Phase 5R.2 allowlist",
        )
    return ClassifiedTool(tool, ToolClass.UNKNOWN, "", (), "unrecognized tool")


def classify_tools_list(tools: Any) -> list[ClassifiedTool]:
    """Parse MCP ``tools/list`` (or docs fixture) into classified rows."""
    rows: list[Any]
    if isinstance(tools, dict):
        rows = list(tools.get("tools") or tools.get("result", {}).get("tools") or [])
    elif isinstance(tools, list):
        rows = list(tools)
    else:
        rows = []

    out: list[ClassifiedTool] = []
    for row in rows:
        if isinstance(row, str):
            base = classify_tool_name(row)
            out.append(base)
            continue
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("tool") or "").strip()
        desc = str(row.get("description") or "")
        schema = row.get("inputSchema") or row.get("input_schema") or row.get("parameters") or {}
        props: tuple[str, ...] = ()
        if isinstance(schema, dict):
            raw_props = schema.get("properties") or schema.get("fields") or {}
            if isinstance(raw_props, dict):
                props = tuple(sorted(str(k) for k in raw_props))
        base = classify_tool_name(name)
        out.append(
            ClassifiedTool(
                name=base.name,
                classification=base.classification,
                description=desc,
                property_keys=props,
                reason=base.reason,
            )
        )
    return sorted(out, key=lambda t: t.name)


def assert_preflight_tool_allowed(name: str) -> AllowlistDecision:
    """Allow only Phase 5R.2 READ_ONLY tools. UNKNOWN and MUTATING refuse."""
    classified = classify_tool_name(name)
    if classified.classification is ToolClass.READ_ONLY:
        # Also stay within the existing connected-shadow read client surface
        # when the tool is one of the five snapshot reads.
        if classified.name in ALLOWED_READS or classified.name in PREFLIGHT_READ_ALLOWLIST:
            return AllowlistDecision(
                True,
                classified.name,
                ToolClass.READ_ONLY,
                "allowlisted read-only preflight tool",
            )
    return AllowlistDecision(
        False,
        classified.name or (name or "").strip(),
        classified.classification,
        f"refusing {classified.classification.value}: {classified.reason}",
    )


def assert_preflight_tool_or_raise(name: str) -> str:
    decision = assert_preflight_tool_allowed(name)
    if not decision.allowed:
        raise PermissionError(
            f"Phase 5R.2 read-only preflight refuses tool {decision.tool!r} "
            f"({decision.classification.value}): {decision.reason}"
        )
    return decision.tool


def kill_switches_disarmed(env: Mapping[str, str] | None = None) -> tuple[bool, tuple[str, ...]]:
    """Return (all_false, tripped_names). True means safe (all kill switches off)."""
    source = dict(env or {})
    keys = (
        "ROBINHOOD_HOST_ENABLED",
        "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED",
        "ROBINHOOD_HOST_LIVE_SUBMISSION",
        "ROBINHOOD_HOST_EXECUTOR",
        "LIVE_TRADING_ENABLED",
    )

    def _on(raw: str | None) -> bool:
        return (raw or "").strip().lower() in {"1", "true", "yes", "on"}

    tripped = tuple(k for k in keys if _on(source.get(k)))
    return len(tripped) == 0, tripped


def sanitize_for_report(payload: Any, *, depth: int = 0) -> Any:
    """Redact sensitive keys / long opaque ids for docs and fixtures."""
    if depth > 12:
        return "[truncated]"
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            k = str(key)
            if _SENSITIVE_KEY_RE.search(k):
                out[k] = "[REDACTED]"
            else:
                out[k] = sanitize_for_report(value, depth=depth + 1)
        return out
    if isinstance(payload, list):
        return [sanitize_for_report(item, depth=depth + 1) for item in payload[:50]]
    if isinstance(payload, str) and len(payload) > 64 and re.fullmatch(r"[0-9a-fA-F-]{32,}", payload):
        return "[REDACTED_ID]"
    return payload


class ReadOnlyPreflightClient:
    """Injected-transport client that hard-refuses non-allowlisted tools."""

    def __init__(self, transport) -> None:  # type: ignore[no-untyped-def]
        self._transport = transport
        self.invocations: list[str] = []
        self.rejected: list[str] = []

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        try:
            tool = assert_preflight_tool_or_raise(name)
        except PermissionError:
            self.rejected.append((name or "").strip())
            raise
        self.invocations.append(tool)
        return self._transport(tool, dict(arguments or {}))

    def run_account_reads(
        self,
        *,
        symbols: tuple[str, ...] = ("QQQ", "TQQQ", "SQQQ"),
    ) -> dict[str, Any]:
        """Invoke the narrow read set. Never places/reviews/cancels."""
        if len(symbols) > 20:
            raise ValueError("get_equity_quotes accepts at most 20 symbols")
        return {
            "get_accounts": self.call("get_accounts", {}),
            "get_portfolio": self.call("get_portfolio", {}),
            "get_equity_positions": self.call("get_equity_positions", {}),
            "get_equity_quotes": self.call(
                "get_equity_quotes", {"symbols": list(symbols)}
            ),
            "get_equity_orders": self.call("get_equity_orders", {}),
            "get_trade_approval_setting": self.call("get_trade_approval_setting", {}),
        }


def auth_gate_status(
    *,
    interactive_oauth_complete: bool,
    self_hosted_workers: int = 0,
    rh_mcp_namespace_present: bool = False,
) -> dict[str, Any]:
    """Describe whether live tools/list may proceed. Never invents auth."""
    if interactive_oauth_complete and (rh_mcp_namespace_present or self_hosted_workers > 0):
        return {
            "status": "READY",
            "auth_case": AUTH_CASE,
            "unattended_auth_supported": UNATTENDED_AUTH_SUPPORTED,
            "detail": "interactive OAuth complete on approved host",
        }
    return {
        "status": "BLOCKED",
        "auth_case": AUTH_CASE,
        "unattended_auth_supported": UNATTENDED_AUTH_SUPPORTED,
        "official_mcp_url": OFFICIAL_MCP_URL,
        "self_hosted_workers": self_hosted_workers,
        "rh_mcp_namespace_present": rh_mcp_namespace_present,
        "detail": (
            "Interactive OAuth required on an approved MCP host "
            "(Cursor Desktop / Claude / Codex with RH Trading MCP). "
            "This cloud VM has no RH MCP namespace and no self-hosted workers; "
            "unattended client_credentials is unsupported."
        ),
    }


__all__ = [
    "ALLOWED_READS",
    "AUTH_CASE",
    "AllowlistDecision",
    "ClassifiedTool",
    "OFFICIAL_MCP_URL",
    "PREFLIGHT_MUTATING_DENYLIST",
    "PREFLIGHT_READ_ALLOWLIST",
    "ReadOnlyPreflightClient",
    "ToolClass",
    "UNATTENDED_AUTH_SUPPORTED",
    "assert_preflight_tool_allowed",
    "assert_preflight_tool_or_raise",
    "auth_gate_status",
    "classify_tool_name",
    "classify_tools_list",
    "kill_switches_disarmed",
    "sanitize_for_report",
]
