"""Phase 5R.3 — deterministic **mock** Robinhood host execution engine.

Paper / simulation only. Never opens a socket to Robinhood. Never calls the
live MCP place/review/cancel transport. Every fill is tagged
``fill_source=SIMULATED_MOCK`` so it cannot be mistaken for a real RH fill.

Intended use:
- Inject as ``HostTransport`` into ``HostMediatedClient`` / ``run_host_executor_once``
- Drive offline pytest + local rehearsal against InMemory intent/reservation stores
- Do **not** write simulated orders into production Supabase execution tables

Absolute refusals:
- Real RH place/review/cancel
- Targeting protected accounts (margin …9384, Roth …5767)
- Labeling simulated fills as broker fills without the SIMULATED_MOCK marker
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from robinhood_account_isolation import (
    PROTECTED_LAST4,
    AccountIsolationError,
    BoundAgenticAccount,
    assert_account_allowed,
    is_protected_account_number,
    mask_account_number,
    require_bound_account,
)
from robinhood_flip import advance_flip, recovery_block_reason
from robinhood_host_executor import HostTransportError

# Explicit marker — never omit from place responses.
FILL_SOURCE_SIMULATED = "SIMULATED_MOCK"
SIM_ORDER_ID_PREFIX = "sim-mock-"

DEFAULT_AGENTIC_ACCOUNT = "TESTAGT6650"
DEFAULT_PROTECTED_MARGIN = "TESTMRG9384"
DEFAULT_PROTECTED_ROTH = "TESTIRA5767"

ALLOWED_SIM_SYMBOLS = frozenset({"TQQQ", "SQQQ"})


class SimOutcome(str, Enum):
    """Deterministic place outcome overrides for tests."""

    FILL = "FILL"
    PARTIAL = "PARTIAL"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    EXPIRE = "EXPIRE"
    DELAY_THEN_FILL = "DELAY_THEN_FILL"
    NETWORK_ERROR = "NETWORK_ERROR"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT_BP = "INSUFFICIENT_BP"
    MARKET_CLOSED = "MARKET_CLOSED"
    STALE_QUOTE = "STALE_QUOTE"


@dataclass
class SimQuote:
    symbol: str
    bid: float
    ask: float
    last: float
    quote_time: datetime


@dataclass
class SimOrder:
    order_id: str
    account_number: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: float | None
    status: str
    filled_qty: float
    fill_source: str = FILL_SOURCE_SIMULATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client_signal_id: str | None = None


@dataclass
class MockBrokerBook:
    """In-memory cash + single-ETF book for paper simulation."""

    cash: float = 10_000.0
    equity: float = 10_000.0
    buying_power: float = 10_000.0
    day_start_equity: float = 10_000.0
    week_start_equity: float = 10_000.0
    peak_equity: float = 10_000.0
    positions: dict[str, float] = field(default_factory=dict)  # symbol -> qty
    market_open: bool = True

    def mark_to_market(self, quotes: Mapping[str, SimQuote]) -> None:
        pos_value = 0.0
        for sym, qty in self.positions.items():
            q = quotes.get(sym)
            if q is None or qty == 0:
                continue
            mid = (q.bid + q.ask) / 2.0
            pos_value += qty * mid
        self.equity = self.cash + pos_value
        self.buying_power = max(0.0, self.cash)
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity


class MockRobinhoodExecutionEngine:
    """Stateful mock HostTransport for Phase 5R.3 paper simulation.

    Thread-safe enough for concurrent claim/place tests (RLock around book).
    """

    REAL_MONEY = False
    REAL_ORDERS = False

    def __init__(
        self,
        *,
        agentic_account: str = DEFAULT_AGENTIC_ACCOUNT,
        cash: float = 10_000.0,
        now: datetime | None = None,
        default_outcome: SimOutcome = SimOutcome.FILL,
        delay_seconds: float = 0.0,
        include_baselines: bool = True,
    ) -> None:
        self.now = now or datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
        self.agentic_account = agentic_account
        self.default_outcome = default_outcome
        self.delay_seconds = delay_seconds
        self.include_baselines = include_baselines
        self._lock = threading.RLock()
        self._id_seq = itertools.count(1)
        self.book = MockBrokerBook(
            cash=cash,
            equity=cash,
            buying_power=cash,
            day_start_equity=cash,
            week_start_equity=cash,
            peak_equity=cash,
        )
        self.quotes: dict[str, SimQuote] = {
            "TQQQ": SimQuote("TQQQ", 50.0, 50.10, 50.05, self.now),
            "SQQQ": SimQuote("SQQQ", 20.0, 20.05, 20.02, self.now),
            "QQQ": SimQuote("QQQ", 480.0, 480.20, 480.10, self.now),
        }
        self.orders: dict[str, SimOrder] = {}
        self.open_order_ids: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.place_attempts = 0
        self.review_attempts = 0
        self.cancel_attempts = 0
        self.real_rh_place_attempts = 0  # must always stay 0
        self.outcome_queue: list[SimOutcome] = []
        self.force_stale_quotes = False
        self.force_review_decision = ""
        self.network_fail_next_n = 0
        self._seen_place_fingerprints: set[str] = set()
        self.duplicate_place_rejections = 0

    # --- configuration helpers -------------------------------------------------

    def enqueue_outcome(self, outcome: SimOutcome) -> None:
        self.outcome_queue.append(outcome)

    def set_position(self, symbol: str, qty: float) -> None:
        with self._lock:
            sym = symbol.upper()
            if qty <= 0:
                self.book.positions.pop(sym, None)
            else:
                self.book.positions[sym] = float(qty)
            self.book.mark_to_market(self.quotes)

    def set_quote(
        self,
        symbol: str,
        *,
        bid: float,
        ask: float,
        last: float | None = None,
        quote_time: datetime | None = None,
    ) -> None:
        with self._lock:
            sym = symbol.upper()
            self.quotes[sym] = SimQuote(
                sym,
                bid,
                ask,
                last if last is not None else (bid + ask) / 2.0,
                quote_time or self.now,
            )
            self.book.mark_to_market(self.quotes)

    def set_market_open(self, open_: bool) -> None:
        self.book.market_open = open_

    def bound_agentic(self) -> BoundAgenticAccount:
        return BoundAgenticAccount(
            account_number=self.agentic_account,
            nickname="Agentic",
            last4=self.agentic_account[-4:],
            brokerage_account_type="individual",
            account_type="cash",
            source="mock_engine",
        )

    def accounts_payload(self) -> dict[str, Any]:
        return {
            "accounts": [
                {
                    "account_number": self.agentic_account,
                    "nickname": "Agentic",
                    "type": "cash",
                    "brokerage_account_type": "individual",
                    "agentic_allowed": True,
                    "state": "active",
                    "status": "active",
                },
                {
                    "account_number": DEFAULT_PROTECTED_MARGIN,
                    "nickname": "Primary",
                    "type": "margin",
                    "brokerage_account_type": "individual",
                    "agentic_allowed": False,
                    "state": "active",
                    "status": "active",
                },
                {
                    "account_number": DEFAULT_PROTECTED_ROTH,
                    "nickname": "Roth",
                    "type": "cash",
                    "brokerage_account_type": "ira_roth",
                    "agentic_allowed": False,
                    "state": "active",
                    "status": "active",
                },
            ]
        }

    def money_moved(self) -> float:
        """Real money moved — always $0.00 for this engine."""
        return 0.0

    def real_orders_placed(self) -> int:
        return self.real_rh_place_attempts

    # --- HostTransport ---------------------------------------------------------

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = (name or "").strip()
        args = dict(arguments or {})
        self.calls.append((tool, args))
        with self._lock:
            if self.network_fail_next_n > 0:
                self.network_fail_next_n -= 1
                raise HostTransportError("simulated network failure: connection reset")
            if tool == "get_accounts":
                return self.accounts_payload()
            if tool == "get_portfolio":
                self._require_account(args)
                return self._portfolio()
            if tool == "get_equity_positions":
                self._require_account(args)
                return self._positions()
            if tool == "get_equity_orders":
                self._require_account(args)
                return self._orders_payload()
            if tool == "get_equity_quotes":
                return self._quotes_payload(args.get("symbols") or [])
            if tool == "get_trade_approval_setting":
                self._require_account(args)
                return {"trade_approvals_enabled": True, "human_must_approve_trades": True}
            if tool == "review_equity_order":
                self.review_attempts += 1
                return self._review(args)
            if tool == "place_equity_order":
                self.place_attempts += 1
                # This engine is the mock — never increments real_rh_place_attempts.
                return self._place(args)
            if tool == "cancel_equity_order":
                self.cancel_attempts += 1
                return self._cancel(args)
            raise HostTransportError(f"mock engine does not implement tool {tool!r}")

    # --- internals -------------------------------------------------------------

    def _require_account(self, args: Mapping[str, Any]) -> str:
        raw = str(args.get("account_number") or "").strip()
        if not raw:
            raise AccountIsolationError(
                "account_number missing; mock refuses unbound account-scoped call"
            )
        if is_protected_account_number(raw):
            raise AccountIsolationError(
                f"refusing protected account {mask_account_number(raw)} "
                f"(suffix in {sorted(PROTECTED_LAST4)})"
            )
        bound = self.bound_agentic()
        return assert_account_allowed(raw, bound)

    def _portfolio(self) -> dict[str, Any]:
        self.book.mark_to_market(self.quotes)
        out: dict[str, Any] = {
            "equity": self.book.equity,
            "buying_power": self.book.buying_power,
            "cash": self.book.cash,
            "total_value": self.book.equity,
            "simulation": True,
            "fill_source": FILL_SOURCE_SIMULATED,
        }
        if self.include_baselines:
            out.update(
                {
                    "day_start_equity": self.book.day_start_equity,
                    "week_start_equity": self.book.week_start_equity,
                    "peak_equity": self.book.peak_equity,
                }
            )
        return out

    def _positions(self) -> dict[str, Any]:
        rows = [
            {
                "symbol": sym,
                "quantity": qty,
                "qty": qty,
                "simulation": True,
                "fill_source": FILL_SOURCE_SIMULATED,
            }
            for sym, qty in sorted(self.book.positions.items())
            if qty > 0
        ]
        return {"positions": rows}

    def _orders_payload(self) -> dict[str, Any]:
        rows = []
        for oid in list(self.open_order_ids) + [
            o.order_id for o in self.orders.values() if o.order_id not in self.open_order_ids
        ]:
            order = self.orders.get(oid)
            if order is None:
                continue
            rows.append(
                {
                    "id": order.order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "status": order.status,
                    "quantity": order.quantity,
                    "filled_qty": order.filled_qty,
                    "order_type": order.order_type,
                    "limit_price": order.limit_price,
                    "account_number": order.account_number,
                    "created_at": order.created_at.isoformat(),
                    "updated_at": order.created_at.isoformat(),
                    "fill_source": FILL_SOURCE_SIMULATED,
                    "simulation": True,
                    "real_rh_fill": False,
                }
            )
        return {"orders": rows}

    def _quotes_payload(self, symbols: Any) -> dict[str, Any]:
        wanted = [str(s).upper() for s in (symbols or [])]
        if not wanted:
            wanted = ["TQQQ", "SQQQ"]
        out = []
        for sym in wanted:
            q = self.quotes.get(sym)
            if q is None:
                continue
            qt = q.quote_time
            if self.force_stale_quotes:
                # Far in the past so freshness gates trip.
                qt = datetime(2020, 1, 1, tzinfo=timezone.utc)
            out.append(
                {
                    "symbol": sym,
                    "bid": q.bid,
                    "ask": q.ask,
                    "last": q.last,
                    "quote_time": qt.isoformat(),
                    "simulation": True,
                }
            )
        return {"quotes": out}

    def _next_outcome(self) -> SimOutcome:
        if self.outcome_queue:
            return self.outcome_queue.pop(0)
        return self.default_outcome

    def _review(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self._require_account(args)
        symbol = str(args.get("symbol") or "").upper()
        side = str(args.get("side") or "").lower()
        warnings: list[str] = []
        if symbol not in ALLOWED_SIM_SYMBOLS:
            warnings.append(f"symbol {symbol} not on TQQQ/SQQQ allowlist")
        if side not in {"buy", "sell"}:
            warnings.append(f"unsupported side {side}")
        if not self.book.market_open:
            warnings.append("market closed")
        # Optional forced review outcome for H4 tests (fixture schema only).
        forced = str(getattr(self, "force_review_decision", "") or "").strip().upper()
        if forced in {"REJECTED", "PENDING", "EXPIRED", "UNKNOWN", "APPROVED"}:
            ok = forced == "APPROVED"
            return {
                "ok": ok,
                "decision": forced,
                "status": forced,
                "warnings": warnings if not ok else [],
                "simulation": True,
                "fill_source": FILL_SOURCE_SIMULATED,
            }
        ok = len(warnings) == 0
        return {
            "ok": ok,
            "decision": "APPROVED" if ok else "REJECTED",
            "status": "APPROVED" if ok else "REJECTED",
            "warnings": warnings,
            "simulation": True,
            "fill_source": FILL_SOURCE_SIMULATED,
        }

    def _place(self, args: Mapping[str, Any]) -> dict[str, Any]:
        account = self._require_account(args)
        symbol = str(args.get("symbol") or "").upper()
        side = str(args.get("side") or "").lower()
        order_type = str(
            args.get("order_type") or args.get("type") or "limit"
        ).strip().lower()
        try:
            quantity = float(args.get("quantity") or 0)
        except (TypeError, ValueError) as exc:
            raise HostTransportError(f"malformed quantity: {exc}") from None
        limit_raw = args.get("limit_price")
        try:
            limit_price = float(limit_raw) if limit_raw is not None else None
        except (TypeError, ValueError):
            limit_price = None

        if symbol not in ALLOWED_SIM_SYMBOLS:
            return self._reject_payload(account, symbol, side, order_type, quantity, limit_price, "symbol not allowlisted")
        if side not in {"buy", "sell"}:
            return self._reject_payload(account, symbol, side, order_type, quantity, limit_price, "bad side")
        if quantity <= 0:
            return self._reject_payload(account, symbol, side, order_type, quantity, limit_price, "qty<=0")

        # Idempotency fingerprint for duplicate place attempts (symbol+side+qty+limit).
        fingerprint = f"{account}|{symbol}|{side}|{quantity}|{limit_price}|{order_type}"
        if fingerprint in self._seen_place_fingerprints:
            self.duplicate_place_rejections += 1
            return self._reject_payload(
                account, symbol, side, order_type, quantity, limit_price, "duplicate place fingerprint"
            )

        outcome = self._next_outcome()
        if outcome is SimOutcome.NETWORK_ERROR:
            raise HostTransportError("simulated network failure: timeout talking to MCP")
        if outcome is SimOutcome.AMBIGUOUS:
            return {
                "status": "UNKNOWN",
                "fill_source": FILL_SOURCE_SIMULATED,
                "simulation": True,
                "real_rh_fill": False,
            }
        if outcome is SimOutcome.MARKET_CLOSED or not self.book.market_open:
            return self._reject_payload(
                account, symbol, side, order_type, quantity, limit_price, "market closed"
            )
        if outcome is SimOutcome.STALE_QUOTE or self.force_stale_quotes:
            return self._reject_payload(
                account, symbol, side, order_type, quantity, limit_price, "stale quote"
            )

        quote = self.quotes.get(symbol)
        if quote is None:
            return self._reject_payload(
                account, symbol, side, order_type, quantity, limit_price, "no quote"
            )

        fill_px = float(limit_price) if limit_price is not None else (
            quote.ask if side == "buy" else quote.bid
        )
        if order_type == "market":
            fill_px = quote.ask if side == "buy" else quote.bid

        notional = quantity * fill_px
        if side == "buy" and (
            outcome is SimOutcome.INSUFFICIENT_BP or notional > self.book.buying_power + 1e-9
        ):
            return self._reject_payload(
                account,
                symbol,
                side,
                order_type,
                quantity,
                limit_price,
                "insufficient buying power",
            )
        if side == "sell":
            held = float(self.book.positions.get(symbol) or 0.0)
            if quantity > held + 1e-9:
                return self._reject_payload(
                    account,
                    symbol,
                    side,
                    order_type,
                    quantity,
                    limit_price,
                    "insufficient shares",
                )

        if outcome is SimOutcome.DELAY_THEN_FILL and self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        order_id = f"{SIM_ORDER_ID_PREFIX}{next(self._id_seq)}"
        status = "FILLED"
        filled_qty = quantity
        if outcome is SimOutcome.PARTIAL:
            status = "PARTIALLY_FILLED"
            filled_qty = max(0.0, quantity / 2.0)
        elif outcome is SimOutcome.REJECT:
            return self._reject_payload(
                account, symbol, side, order_type, quantity, limit_price, "broker reject"
            )
        elif outcome is SimOutcome.CANCEL:
            status = "CANCELLED"
            filled_qty = 0.0
        elif outcome is SimOutcome.EXPIRE:
            status = "EXPIRED"
            filled_qty = 0.0
        elif outcome in {SimOutcome.FILL, SimOutcome.DELAY_THEN_FILL}:
            status = "FILLED"
            filled_qty = quantity

        order = SimOrder(
            order_id=order_id,
            account_number=account,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status=status,
            filled_qty=filled_qty,
            created_at=self.now,
        )
        self.orders[order_id] = order
        # Fingerprint only committed fills so reject/expire/cancel can be re-tried
        # with the same args in deterministic scenario tests.
        if status in {"FILLED", "PARTIALLY_FILLED"}:
            self._seen_place_fingerprints.add(fingerprint)

        if status == "PARTIALLY_FILLED":
            self._apply_fill(side, symbol, filled_qty, fill_px)
            self.open_order_ids.append(order_id)
        elif status == "FILLED":
            self._apply_fill(side, symbol, filled_qty, fill_px)
        elif status in {"CANCELLED", "EXPIRED"}:
            # No fill; leave book unchanged.
            pass

        return {
            "status": status,
            "id": order_id,
            "order_id": order_id,
            "filled_qty": filled_qty,
            "average_price": fill_px if filled_qty else None,
            "fill_source": FILL_SOURCE_SIMULATED,
            "simulation": True,
            "real_rh_fill": False,
            "account_number_masked": mask_account_number(account),
        }

    def _reject_payload(
        self,
        account: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        limit_price: float | None,
        reason: str,
    ) -> dict[str, Any]:
        order_id = f"{SIM_ORDER_ID_PREFIX}{next(self._id_seq)}"
        order = SimOrder(
            order_id=order_id,
            account_number=account,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            status="REJECTED",
            filled_qty=0.0,
            created_at=self.now,
        )
        self.orders[order_id] = order
        return {
            "status": "REJECTED",
            "id": order_id,
            "order_id": order_id,
            "filled_qty": 0.0,
            "detail": reason,
            "fill_source": FILL_SOURCE_SIMULATED,
            "simulation": True,
            "real_rh_fill": False,
            "account_number_masked": mask_account_number(account),
        }

    def _apply_fill(self, side: str, symbol: str, qty: float, price: float) -> None:
        if qty <= 0:
            return
        if side == "buy":
            cost = qty * price
            self.book.cash -= cost
            self.book.positions[symbol] = float(self.book.positions.get(symbol) or 0.0) + qty
        else:
            proceeds = qty * price
            self.book.cash += proceeds
            held = float(self.book.positions.get(symbol) or 0.0) - qty
            if held <= 1e-9:
                self.book.positions.pop(symbol, None)
            else:
                self.book.positions[symbol] = held
        self.book.mark_to_market(self.quotes)

    def _cancel(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self._require_account(args)
        oid = str(args.get("order_id") or args.get("id") or "").strip()
        order = self.orders.get(oid)
        if order is None:
            return {
                "ok": False,
                "detail": "unknown order",
                "fill_source": FILL_SOURCE_SIMULATED,
                "simulation": True,
            }
        if order.status in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
            return {
                "ok": False,
                "detail": f"already {order.status}",
                "fill_source": FILL_SOURCE_SIMULATED,
                "simulation": True,
            }
        order = replace(order, status="CANCELLED")
        self.orders[oid] = order
        if oid in self.open_order_ids:
            self.open_order_ids.remove(oid)
        return {
            "ok": True,
            "status": "CANCELLED",
            "id": oid,
            "fill_source": FILL_SOURCE_SIMULATED,
            "simulation": True,
            "real_rh_fill": False,
        }

    # --- FLIP helpers (validation only; still mock) ----------------------------

    def flip_entry_allowed(
        self,
        *,
        exit_status: str,
        exit_symbol: str,
    ) -> tuple[bool, str]:
        """Confirmed flat close before opposite buy — reuses production flip gate."""
        remaining = float(self.book.positions.get(exit_symbol.upper()) or 0.0)
        gate = advance_flip(
            exit_status=exit_status,
            exit_qty_remaining=remaining,
            broker_state_known=True,
        )
        return gate.entry_validation_allowed, gate.reason

    def recovery_gate(
        self,
        *,
        proposed_client_order_id: str,
        known_client_order_ids: frozenset[str],
        broker_state_known: bool = True,
    ) -> str | None:
        return recovery_block_reason(
            open_order_count=len(self.open_order_ids),
            proposed_client_order_id=proposed_client_order_id,
            known_client_order_ids=known_client_order_ids,
            broker_state_known=broker_state_known,
        )


def resolve_mock_agentic_or_raise(payload: Any) -> BoundAgenticAccount:
    """Test helper: bind Agentic from mock get_accounts; fail closed otherwise."""
    return require_bound_account(get_accounts_payload=payload)


__all__ = [
    "ALLOWED_SIM_SYMBOLS",
    "DEFAULT_AGENTIC_ACCOUNT",
    "DEFAULT_PROTECTED_MARGIN",
    "DEFAULT_PROTECTED_ROTH",
    "FILL_SOURCE_SIMULATED",
    "MockBrokerBook",
    "MockRobinhoodExecutionEngine",
    "SIM_ORDER_ID_PREFIX",
    "SimOrder",
    "SimOutcome",
    "SimQuote",
    "resolve_mock_agentic_or_raise",
]
