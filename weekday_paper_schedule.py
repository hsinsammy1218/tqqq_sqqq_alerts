"""Weekday Eastern slot helpers for local paper-learning runs.

CLI entrypoint: ``python scripts/run_weekday_paper.py``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# Hourly 10:00–15:00 ET weekdays + near-close 15:30 (7 scans/weekday).
DEFAULT_SLOTS: tuple[tuple[int, int], ...] = (
    (10, 0),
    (11, 0),
    (12, 0),
    (13, 0),
    (14, 0),
    (15, 0),
    (15, 30),
)


def entrypoint_cmd() -> tuple[str, ...]:
    return (sys.executable, "main.py", "--no-technical", "--market-hours-only")


@dataclass(frozen=True)
class Slot:
    hour: int
    minute: int

    def as_time(self) -> dt_time:
        return dt_time(self.hour, self.minute, tzinfo=ET)


def parse_slots(raw: str | None) -> tuple[Slot, ...]:
    if not raw or not raw.strip():
        return tuple(Slot(h, m) for h, m in DEFAULT_SLOTS)
    out: list[Slot] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, mm = part.split(":", 1)
        out.append(Slot(int(hh), int(mm)))
    if not out:
        raise ValueError("No slots parsed from --slots")
    return tuple(out)


def next_slot_after(now: datetime, slots: tuple[Slot, ...]) -> datetime:
    """Return the next weekday Eastern slot at or after ``now`` (minute resolution)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(ET)
    cursor = local.replace(second=0, microsecond=0)
    day = cursor.date()
    for _ in range(14):  # two weeks max search
        if day.weekday() < 5:  # Mon–Fri
            for slot in sorted(slots, key=lambda s: (s.hour, s.minute)):
                candidate = datetime.combine(day, slot.as_time())
                if candidate >= cursor:
                    return candidate
        day = day + timedelta(days=1)
        cursor = datetime.combine(day, dt_time(0, 0, tzinfo=ET))
    raise RuntimeError("Could not find next weekday slot")


def utc_cron_for_eastern_slot(hour: int, minute: int, *, edt: bool = True) -> str:
    """Approximate UTC crontab for an Eastern wall-clock slot (EDT=UTC-4, EST=UTC-5)."""
    offset = 4 if edt else 5
    utc_hour = (hour + offset) % 24
    return f"{minute} {utc_hour} * * 1-5"


def repo_root_from_script(script_file: str | Path) -> Path:
    return Path(script_file).resolve().parent.parent


def run_entrypoint(cwd: Path) -> int:
    env = os.environ.copy()
    cmd = entrypoint_cmd()
    print(
        f"[weekday-paper] {datetime.now(tz=ET).isoformat()} starting: {' '.join(cmd)}",
        flush=True,
    )
    proc = subprocess.run(cmd, cwd=str(cwd), env=env)
    print(f"[weekday-paper] exit_code={proc.returncode}", flush=True)
    return int(proc.returncode)


def print_cron_lines(slots: tuple[Slot, ...], repo: Path) -> None:
    print("# Local durable learning (system crontab; uses machine local TZ — prefer --loop or ET-aware runner)")
    print(f"# Repo: {repo}")
    print("# Recommended: leave `python scripts/run_weekday_paper.py --loop` running under tmux/systemd.")
    print("# Equivalent weekday Eastern slots via helper --once (set TZ or rely on helper ET math):")
    for slot in slots:
        print(
            f"# {slot.hour:02d}:{slot.minute:02d} America/New_York weekdays → "
            f"cd {repo} && python scripts/run_weekday_paper.py --once"
        )
    print()
    print("# If your cron daemon is UTC and you want raw main.py (EDT / UTC-4):")
    for slot in slots:
        expr = utc_cron_for_eastern_slot(slot.hour, slot.minute, edt=True)
        print(
            f"{expr} cd {repo} && python main.py --no-technical --market-hours-only "
            f">> logs/weekday_paper_cron.log 2>&1"
        )
    print()
    print("# EST winter (UTC-5) — shift hours +1 vs EDT lines above, or use --loop.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Local weekday paper-alert scheduler for trade-learning accumulation. "
            "Runs: python main.py --no-technical --market-hours-only "
            "hourly 10:00–15:00 plus 15:30 America/New_York on weekdays."
        )
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run the paper entrypoint once and exit.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Sleep until each weekday Eastern slot and run (leave this running).",
    )
    mode.add_argument(
        "--print-cron",
        action="store_true",
        help="Print example crontab lines and exit (no network).",
    )
    p.add_argument(
        "--slots",
        default=None,
        help="Comma-separated HH:MM Eastern times (default 10:00,11:00,12:00,13:00,14:00,15:00,15:30).",
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=20,
        help="Loop wake interval while waiting for a slot (default 20).",
    )
    return p


def main(argv: list[str] | None = None, *, script_file: str | Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    slots = parse_slots(args.slots)
    root = repo_root_from_script(script_file or Path(__file__))
    if script_file is None and Path(__file__).name == "weekday_paper_schedule.py":
        # When imported/run as module from repo root, root is parent of this file.
        root = Path(__file__).resolve().parent
    os.chdir(root)

    if args.print_cron:
        print_cron_lines(slots, root)
        return 0

    if args.once:
        return run_entrypoint(root)

    last_fired: date | None = None
    last_hm: tuple[int, int] | None = None
    print(
        f"[weekday-paper] loop started; slots={[f'{s.hour:02d}:{s.minute:02d}' for s in slots]} ET weekdays",
        flush=True,
    )
    while True:
        now = datetime.now(tz=ET)
        target = next_slot_after(now, slots)
        wait = (target - now).total_seconds()
        if wait > 1:
            sleep_for = min(float(args.poll_seconds), wait)
            time.sleep(max(0.5, sleep_for))
            continue
        hm = (target.hour, target.minute)
        if last_fired == target.date() and last_hm == hm:
            time.sleep(float(args.poll_seconds))
            continue
        code = run_entrypoint(root)
        last_fired = target.date()
        last_hm = hm
        if code != 0:
            print(f"[weekday-paper] WARNING: entrypoint failed with {code}; continuing loop", flush=True)
        time.sleep(61)
