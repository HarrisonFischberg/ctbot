#!/bin/bash
# Start CTbot (./run_ctbot.sh: dashboard + paper bot) at login and restart it if it stops,
# with a macOS launchd agent (com.ctbot.bot). Output goes to logs/bot.log.
#   scripts/autostart.sh install     install or refresh the agent and start it now
#   scripts/autostart.sh uninstall   stop it and remove the agent
#   scripts/autostart.sh status      is it loaded, and its pid
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD
LABEL=com.ctbot.bot
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)

if ! command -v launchctl >/dev/null; then
  echo "launchctl not found: autostart needs macOS. Run ./run_ctbot.sh yourself instead." >&2
  exit 1
fi

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-}" in
  install)
    if [ ! -x .venv/bin/ctbot-paper ]; then
      echo "No .venv/bin/ctbot-paper yet: do the Quick start install in README.md first." >&2
      exit 1
    fi
    case "$ROOT" in *'#'*|*'&'*|*'<'*)
      echo "The project folder path has #, & or <; move it to a plainer path first." >&2
      exit 1 ;;
    esac
    # Two paper bots on one account would double every order, so refuse while one runs by hand.
    if ! loaded && pgrep -f "$ROOT/.venv/bin/ctbot-paper" >/dev/null; then
      echo "CTbot is already running (./run_ctbot.sh?). Stop it with Ctrl+C, then run install again." >&2
      exit 1
    fi
    mkdir -p logs "$HOME/Library/LaunchAgents"
    sed "s#/path/to/ctbot#$ROOT#g" scripts/$LABEL.plist.example > "$PLIST"
    plutil -lint "$PLIST" >/dev/null || { echo "Generated $PLIST is not a valid plist." >&2; exit 1; }
    loaded && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    launchctl bootstrap "$DOMAIN" "$PLIST" || exit 1
    echo "Autostart on: CTbot starts at login and restarts if it stops."
    echo "Dashboard: http://127.0.0.1:8787 · log: logs/bot.log · turn off: scripts/autostart.sh uninstall"
    ;;
  uninstall)
    loaded && launchctl bootout "$DOMAIN/$LABEL"
    rm -f "$PLIST"
    echo "Autostart off: CTbot stopped and won't start at login."
    ;;
  status)
    if loaded; then
      launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|pid|last exit code) =' | sed 's/^[[:space:]]*//'
    else
      echo "Autostart is off."
    fi
    ;;
  *)
    echo "usage: scripts/autostart.sh install | uninstall | status" >&2
    exit 2
    ;;
esac
