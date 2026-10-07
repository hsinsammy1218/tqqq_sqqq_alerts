#!/usr/bin/env python3
"""Execute one pending Robinhood host-mediated intent.

Run ONLY inside an authenticated Robinhood MCP host environment that can
inject a real host transport. This repository's unattended Render cron must
not call place_equity_order (Case C).

By default this CLI refuses to run without:
  ROBINHOOD_HOST_EXECUTOR=true
  and an injected transport module path via --transport-factory (tests/local).

It never invents OAuth. Static ROBINHOOD_MCP_TOKEN is ignored.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root or scripts/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_transport(factory: str):
    """factory format: package.module:callable"""
    if ":" not in factory:
        raise SystemExit("--transport-factory must be module:callable")
    mod_name, fn_name = factory.split(":", 1)
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    transport = fn()
    if transport is None:
        raise SystemExit("transport factory returned None")
    return transport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport-factory",
        required=True,
        help="Python module:callable that returns an injected HostTransport. "
        "Official auth is the MCP host session — do not pass a static bearer.",
    )
    parser.add_argument("--client-order-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL", ""))
    args = parser.parse_args(argv)

    if (os.environ.get("ROBINHOOD_HOST_EXECUTOR") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print("Refusing: set ROBINHOOD_HOST_EXECUTOR=true in the authenticated host env.")
        return 2
    if (os.environ.get("RENDER") or "").strip() or (os.environ.get("RENDER_SERVICE_TYPE") or "").strip():
        print("Refusing: Render cannot submit Robinhood orders under Case C.")
        return 2

    from robinhood_host_executor import run_host_executor_once
    from robinhood_intent_store import intent_store_from_env

    transport = _load_transport(args.transport_factory)
    try:
        store = intent_store_from_env()
    except RuntimeError as exc:
        print(f"Supabase required for host execution: {exc}")
        return 2

    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=os.environ,
        now=datetime.now(timezone.utc),
        client_order_id=args.client_order_id,
        dry_run=args.dry_run,
        webhook_url=args.webhook_url,
    )
    print(result.text)
    return 0 if result.legs else 1


if __name__ == "__main__":
    raise SystemExit(main())
