#!/usr/bin/env bash
# Start a paper-trading session from the project folder:  ./run_ctbot.sh
#   1. the dashboard at http://127.0.0.1:8787 (background; output in logs/dashboard.log)
#   2. the paper bot, 5-minute cycles, in the foreground
# On macOS, `caffeinate -di` keeps the display and system from idle-sleeping while it runs
# (closing the lid still sleeps a laptop unless it has power and an external display).
# If you set up the optional launchd refresh job (docs/COMMANDS.md), it is kicked off too.
# Ctrl+C stops the bot and the dashboard together.
set -u
cd "$(dirname "$0")" || exit 1
mkdir -p logs
if command -v launchctl >/dev/null; then
  launchctl kickstart "gui/$(id -u)/com.ctbot.c2-track" 2>/dev/null || true
fi
.venv/bin/ctbot-dashboard >> logs/dashboard.log 2>&1 &
DASH=$!
trap 'kill $DASH 2>/dev/null' EXIT INT TERM
echo "dashboard: http://127.0.0.1:8787 (pid $DASH) · paper bot starting · Ctrl+C stops both"
if command -v caffeinate >/dev/null; then
  caffeinate -di .venv/bin/ctbot-paper --loop --interval 300
else
  .venv/bin/ctbot-paper --loop --interval 300
fi
