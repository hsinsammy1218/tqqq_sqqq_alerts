#!/usr/bin/env bash
# One-time install: make weekday paper trading auto-start after reboot/login.
#
# Prefer systemd --user unit when available; otherwise install crontab @reboot.
# Always starts (or confirms) the loop immediately via start_paper_trading.sh.
#
# Usage (from repo root, once):
#   bash scripts/install_paper_autostart.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START="$ROOT/scripts/start_paper_trading.sh"
UNIT_SRC="$ROOT/scripts/paper-trading.service"
MARKER="# tqqq-sqqq-paper-autostart"
CRON_LINE="@reboot $START"

chmod +x "$START" "$ROOT/scripts/install_paper_autostart.sh"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "warning: $ROOT/.env missing — create it with ALPACA_PAPER_TRADING=true and DRY_RUN=false before market open" >&2
fi

install_systemd_user() {
  local unit_dir="$HOME/.config/systemd/user"
  local unit_dst="$unit_dir/paper-trading.service"
  mkdir -p "$unit_dir"
  # Rewrite WorkingDirectory / ExecStart to this checkout (template uses %h/workspace).
  sed \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$ROOT|" \
    -e "s|ExecStart=.*|ExecStart=$START|" \
    "$UNIT_SRC" >"$unit_dst"
  systemctl --user daemon-reload
  systemctl --user enable --now paper-trading.service
  # Survive logout when linger is available (ignore failure on locked-down hosts).
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" 2>/dev/null || true
  fi
  echo "[paper-autostart] installed systemd --user unit: paper-trading.service"
  systemctl --user status paper-trading.service --no-pager || true
}

install_crontab_reboot() {
  if ! command -v crontab >/dev/null 2>&1; then
    echo "error: neither systemd --user nor crontab is available" >&2
    exit 1
  fi
  local existing
  existing="$(crontab -l 2>/dev/null || true)"
  # Drop prior marker lines, then append.
  local filtered
  filtered="$(printf '%s\n' "$existing" | grep -vF "$MARKER" | grep -vF "$START" || true)"
  {
    printf '%s\n' "$filtered"
    echo "$MARKER"
    echo "$CRON_LINE"
  } | grep -v '^$' | crontab -
  echo "[paper-autostart] installed crontab @reboot → $START"
  crontab -l | sed -n "/$MARKER/,+1p"
}

# Prefer systemd user units when the user bus actually works.
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  install_systemd_user
else
  echo "[paper-autostart] systemd --user unavailable; using crontab @reboot"
  install_crontab_reboot
fi

# Start now (idempotent) regardless of install path.
bash "$START"
echo "[paper-autostart] done. Attach: tmux attach -t weekday-paper-loop"
echo "[paper-autostart] Reminder: sync Render Blueprint (render.yaml) for cloud crons + paper env group."
