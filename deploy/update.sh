#!/bin/bash
# Deploy the latest main if it changed, then restart CTbot. Run as root by ctbot-update.timer on
# weekdays before the open and after the close; it never restarts the bot while the market is open.
#   sudo bash /home/ctbot/ctbot/deploy/update.sh
set -euo pipefail
APP=/home/ctbot/ctbot
BRANCH=${CTBOT_BRANCH:-main}
as_ctbot() { sudo -u ctbot -H "$@"; }
cd "$APP"
if as_ctbot .venv/bin/python -c 'import sys; from datetime import datetime, timezone
from ctbot import market_hours
sys.exit(0 if market_hours.session_open(datetime.now(timezone.utc)) else 1)'; then
  echo "market open: not updating now"
  exit 0
fi
as_ctbot git fetch -q origin "$BRANCH"
if [ "$(as_ctbot git rev-parse HEAD)" = "$(as_ctbot git rev-parse "origin/$BRANCH")" ]; then
  echo "already on the latest $BRANCH"
  exit 0
fi
as_ctbot git pull -q --ff-only origin "$BRANCH"  # before running setup, so the new script runs
echo "updated to $(as_ctbot git log -1 --oneline)"
exec bash "$APP/deploy/setup-server.sh"
