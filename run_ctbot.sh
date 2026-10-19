#!/usr/bin/env bash
# Start a paper-trading session from the project folder:  ./run_ctbot.sh
#   1. the dashboard at http://127.0.0.1:8787 (background; output in logs/dashboard.log),
#      opened in your browser once it answers (skip with CTBOT_NO_BROWSER=1)
#   2. the paper bot, 5-minute cycles, in the foreground
# The bot doesn't keep the Mac awake; use a keep-awake app for market hours. After the close it
# waits for the next open without fetching anything.
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
URL=http://127.0.0.1:8787
echo "dashboard: $URL (pid $DASH) · paper bot starting · Ctrl+C stops both"
if [ -z "${CTBOT_NO_BROWSER:-}" ]; then
  (for _ in $(seq 1 40); do  # wait up to ~20 s for the dashboard, then open it once
     if curl -s -o /dev/null "$URL"; then
       if command -v open >/dev/null; then open "$URL"; elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1; fi
       exit 0
     fi
     sleep 0.5
   done; echo "dashboard didn't answer yet; open $URL yourself") &
fi
.venv/bin/ctbot-paper --loop --interval 300
