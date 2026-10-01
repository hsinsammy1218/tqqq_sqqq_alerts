#!/usr/bin/env bash
# Idempotent: ensure the weekday paper-learning loop is running under tmux.
# Session name: weekday-paper-loop
# Entrypoint: python3 scripts/run_weekday_paper.py --loop (prefers .venv when present)
set -euo pipefail

SESSION="${PAPER_TMUX_SESSION:-weekday-paper-loop}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "error: tmux not found on PATH" >&2
  exit 1
fi

mkdir -p "$ROOT/logs"

# Already running?
if tmux has-session -t "=$SESSION" 2>/dev/null; then
  # Confirm the loop process is alive inside the session (or any pane).
  if pgrep -f "run_weekday_paper.py --loop" >/dev/null 2>&1; then
    echo "[paper-autostart] tmux session '$SESSION' already running (loop alive)"
    exit 0
  fi
  # Session exists but loop died — restart the command in the first pane.
  echo "[paper-autostart] session '$SESSION' present but loop not found; restarting"
  tmux send-keys -t "$SESSION:0.0" C-c "" Enter 2>/dev/null || true
  sleep 0.5
  tmux send-keys -t "$SESSION:0.0" \
    "cd '$ROOT' && '$PY' scripts/run_weekday_paper.py --loop >>logs/weekday_paper_loop.log 2>&1" \
    Enter
  echo "[paper-autostart] restarted loop in existing session '$SESSION'"
  exit 0
fi

tmux new-session -d -s "$SESSION" -c "$ROOT" -- \
  bash -lc "'$PY' scripts/run_weekday_paper.py --loop >>logs/weekday_paper_loop.log 2>&1"
echo "[paper-autostart] started tmux session '$SESSION' with $PY scripts/run_weekday_paper.py --loop"
