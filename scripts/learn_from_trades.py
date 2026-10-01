#!/usr/bin/env python3
"""CLI entry for the read-only learner agent (same as ``python main.py --learn-from-trades``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import run  # noqa: E402


def main() -> int:
    # Preserve any caller flags; ensure --learn-from-trades is present.
    argv = list(sys.argv[1:])
    if "--learn-from-trades" not in argv:
        argv.insert(0, "--learn-from-trades")
    sys.argv = [sys.argv[0], *argv]
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
