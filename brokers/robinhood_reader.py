"""Read-only Robinhood Agentic client.

The transport is injected. This module can call the official read tools and
nothing else. It has no submit, cancel, or replace method. A live flag cannot
add one.

Official read names are from Robinhood's support article "Trading with your
agent" (endpoint https://agent.robinhood.com/mcp/trading). Input schemas are
not published. ``get_equity_quotes`` is documented as accepting up to 20
symbols, so that call sends ``symbols``. Any other failure leaves the caller
with an unknown broker state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

import requests

READ_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
    }
)
OPTIONAL_READ_TOOLS = frozenset({"get_trade_approval_setting"})
ALLOWED_READS = READ_TOOLS | OPTIONAL_READ_TOOLS

Transport = Callable[[str, dict[str, Any]], Any]


class RobinhoodToolRejected(Exception):
    """The caller asked for a tool this client will not invoke."""


class RobinhoodReadError(Exception):
    """A required read failed. Callers must treat broker state as unknown."""


def assert_read_only(name: str) -> str:
    tool = (name or "").strip()
    if tool not in ALLOWED_READS:
        raise RobinhoodToolRejected(
            f"refusing Robinhood tool {tool!r}; this client is read-only"
        )
    return tool


def unwrap_mcp(payload: Any) -> Any:
    """Pull a tool result out of a JSON-RPC or MCP content envelope."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("isError") is True:
        raise RobinhoodReadError("Robinhood read tool returned isError")
    if "result" in payload and "error" not in payload:
        return unwrap_mcp(payload["result"])
    if "error" in payload and "content" not in payload and "structuredContent" not in payload:
        raise RobinhoodReadError("Robinhood read tool returned an error")
    structured = payload.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured
    content = payload.get("content")
    if isinstance(content, list):
        texts = [
            part.get("text")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if len(texts) != 1:
            raise RobinhoodReadError("Robinhood read tool returned unusable content")
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            raise RobinhoodReadError("Robinhood read tool returned non-JSON text") from None
    return payload


class RobinhoodReadClient:
    """Calls allowlisted read tools through an injected transport."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self.invocations: list[str] = []

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = assert_read_only(name)
        self.invocations.append(tool)
        try:
            raw = self._transport(tool, dict(arguments or {}))
        except (RobinhoodToolRejected, RobinhoodReadError):
            raise
        except Exception:
            raise RobinhoodReadError(f"{tool} failed") from None
        return unwrap_mcp(raw)

    def read_snapshot(self, symbols: tuple[str, ...] = ("TQQQ", "SQQQ")) -> dict[str, Any]:
        """Read the five snapshots a connected-shadow decision is allowed to use."""
        if len(symbols) > 20:
            raise RobinhoodReadError("get_equity_quotes accepts at most 20 symbols")
        return {
            "get_accounts": self.call("get_accounts", {}),
            "get_portfolio": self.call("get_portfolio", {}),
            "get_equity_positions": self.call("get_equity_positions", {}),
            "get_equity_quotes": self.call("get_equity_quotes", {"symbols": list(symbols)}),
            "get_equity_orders": self.call("get_equity_orders", {}),
        }


class McpReadTransport:
    """HTTP JSON-RPC transport. The allowlist is checked before any socket open."""

    def __init__(self, url: str, token: str, session: Any | None = None) -> None:
        self._url = url
        self._token = token
        self._session = session

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = assert_read_only(name)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        post = self._session.post if self._session is not None else requests.post
        try:
            response = post(self._url, json=body, headers=headers, timeout=20)
        except requests.RequestException:
            raise RobinhoodReadError(f"{tool} transport failed") from None
        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise RobinhoodReadError(f"{tool} HTTP {status}")
        return _parse_http_payload(response)


def _parse_http_payload(response: Any) -> Any:
    headers = getattr(response, "headers", {}) or {}
    ctype = str(headers.get("content-type", ""))
    text = str(getattr(response, "text", "") or "")
    if "text/event-stream" in ctype or text.lstrip().startswith("data:"):
        parsed: Any = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            blob = line[5:].strip()
            if not blob or blob == "[DONE]":
                continue
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            raise RobinhoodReadError("Robinhood read returned an unreadable event stream")
        return parsed
    try:
        return response.json()
    except ValueError:
        raise RobinhoodReadError("Robinhood read returned non-JSON") from None


def transport_from_env(env: Mapping[str, str] | None = None) -> McpReadTransport | None:
    """Build a transport only when both URL and token are set. Otherwise stay offline."""
    source = os.environ if env is None else env
    url = str(source.get("ROBINHOOD_MCP_URL") or "").strip()
    token = str(source.get("ROBINHOOD_MCP_TOKEN") or "").strip()
    if not url or not token:
        return None
    return McpReadTransport(url, token)
