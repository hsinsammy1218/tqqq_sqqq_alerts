"""Read-only Robinhood Agentic client.

The transport is injected. This module can call the official read tools and
nothing else. It has no submit, cancel, or replace method. A live flag cannot
add one.

Official read names are from Robinhood's support article "Trading with your
agent" (endpoint https://agent.robinhood.com/mcp/trading). Input schemas are
not published. ``get_equity_quotes`` is documented as accepting up to 20
symbols, so that call sends ``symbols``. Any other failure leaves the caller
with an unknown broker state.

Case C: official auth is OAuth inside an MCP host (authorization code + PKCE,
optional refresh after that browser grant). This unattended process has no
host and no client-credentials grant. A static ``ROBINHOOD_MCP_TOKEN`` is not
official auth and is ignored. No socket is opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from robinhood_account_isolation import (
    AccountIsolationError,
    BoundAgenticAccount,
    enforce_tool_account_arg,
    require_bound_account,
)

# Official auth cannot support this runtime. See the module docstring.
AUTH_CASE = "C"
UNATTENDED_AUTH_SUPPORTED = False

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
    """Calls allowlisted read tools through an injected transport.

    Account-scoped tools require a bound Agentic ``account_number`` (Phase 5R.2
    isolation). ``read_snapshot`` resolves via ``get_accounts`` first.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        bound: BoundAgenticAccount | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self.bound = bound
        self._env = env
        self.invocations: list[str] = []

    def bind_from_accounts(self, get_accounts_payload: Any) -> BoundAgenticAccount:
        self.bound = require_bound_account(
            get_accounts_payload=get_accounts_payload,
            env=self._env,
        )
        return self.bound

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = assert_read_only(name)
        try:
            args = enforce_tool_account_arg(tool, arguments, self.bound)
        except AccountIsolationError as exc:
            raise RobinhoodToolRejected(str(exc)) from exc
        self.invocations.append(tool)
        try:
            raw = self._transport(tool, dict(args))
        except (RobinhoodToolRejected, RobinhoodReadError):
            raise
        except Exception:
            raise RobinhoodReadError(f"{tool} failed") from None
        return unwrap_mcp(raw)

    def read_snapshot(self, symbols: tuple[str, ...] = ("TQQQ", "SQQQ")) -> dict[str, Any]:
        """Read the five snapshots a connected-shadow decision is allowed to use.

        Resolves/binds the Agentic account from ``get_accounts`` before any
        account-scoped portfolio/positions/orders call. Quotes are market-data
        (no account_number) but still go through the read-only gate.
        """
        if len(symbols) > 20:
            raise RobinhoodReadError("get_equity_quotes accepts at most 20 symbols")
        accounts = self.call("get_accounts", {})
        try:
            self.bind_from_accounts(accounts)
        except AccountIsolationError as exc:
            raise RobinhoodReadError(str(exc)) from exc
        return {
            "get_accounts": accounts,
            "get_portfolio": self.call("get_portfolio", {}),
            "get_equity_positions": self.call("get_equity_positions", {}),
            "get_equity_quotes": self.call("get_equity_quotes", {"symbols": list(symbols)}),
            "get_equity_orders": self.call("get_equity_orders", {}),
        }


class McpReadTransport:
    """Deprecated static-bearer client. It never opens a socket.

    Robinhood's documented auth is an MCP host OAuth session. A URL plus a
    bearer env var is not that session. Writes are still rejected by name.
    Reads fail closed before any HTTP call.
    """

    def __init__(self, url: str, token: str, session: Any | None = None) -> None:
        # Accepted for old call sites, then dropped. The token is not stored.
        del url, token, session

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        assert_read_only(name)
        raise RobinhoodReadError(
            "unattended Robinhood MCP auth is unsupported; "
            "static bearer JSON-RPC is not used"
        )


def transport_from_env(env: Mapping[str, str] | None = None) -> None:
    """Stay offline. ``ROBINHOOD_MCP_URL`` and ``ROBINHOOD_MCP_TOKEN`` are ignored."""
    del env
    return None
