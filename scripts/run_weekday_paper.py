#!/usr/bin/env python3
"""Local weekday paper-alert scheduler for trade-learning accumulation.

Runs the same entrypoint as Render crons:
  python main.py --no-technical --market-hours-only

Default slots (America/New_York): 10:00, 12:30, 15:30 on weekdays (Mon–Fri).
Appends paper outcomes to logs/trades.jsonl when ALPACA_PAPER_TRADING=true and
DRY_RUN=false in the local .env (do not commit .env).

Usage (from repo root):
  python scripts/run_weekday_paper.py --once          # smoke / manual slot
  python scripts/run_weekday_paper.py --loop           # leave running
  python scripts/run_weekday_paper.py --print-cron     # crontab lines
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script without installing the package.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from weekday_paper_schedule import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(script_file=__file__))
