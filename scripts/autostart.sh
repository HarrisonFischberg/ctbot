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

# Unload the agent and wait until launchd has let go of it: bootout returns before the job is
# gone, and a bootstrap that comes too soon fails with "5: Input/output error".
unload() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
  launchctl unload "$PLIST" 2>/dev/null  # an older `launchctl load` of the same file
  for _ in 1 2 3 4 5 6 7 8 9 10; do loaded || return 0; sleep 0.5; done
  echo "launchd still has $LABEL loaded after bootout." >&2
}

# launchd's own reasons for refusing the agent; "5: Input/output error" alone says nothing.
explain() {
  echo "--- plutil -lint" >&2; plutil -lint "$PLIST" >&2
  echo "--- plist owner and mode (must be you, not group/other writable)" >&2; ls -l "$PLIST" >&2
  echo "--- disabled?" >&2
  launchctl print-disabled "$DOMAIN" 2>&1 | grep -F "$LABEL" >&2 || echo "not disabled" >&2
  echo "--- GUI session domain" >&2
  launchctl print "$DOMAIN" >/dev/null 2>&1 && echo "$DOMAIN ok" >&2 \
    || echo "$DOMAIN unavailable: run this from Terminal on the Mac while logged in, not over ssh" >&2
  echo "--- launchd log, last 2 minutes" >&2
  log show --last 2m --style compact --predicate "eventMessage CONTAINS \"$LABEL\"" 2>/dev/null \
    | tail -n 15 >&2
}

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
    unload
    sed "s#/path/to/ctbot#$ROOT#g" scripts/$LABEL.plist.example > "$PLIST"
    chmod 644 "$PLIST"
    plutil -lint "$PLIST" >/dev/null || { plutil -lint "$PLIST" >&2; exit 1; }
    launchctl enable "$DOMAIN/$LABEL"  # clears an earlier `launchctl disable`
    if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
      sleep 2  # once more, in case launchd was still tearing down an old copy
      if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
        echo "launchctl couldn't load $PLIST. Details:" >&2
        explain
        exit 1
      fi
    fi
    echo "Autostart on: CTbot starts at login and restarts if it stops."
    echo "Dashboard: http://127.0.0.1:8787 · log: logs/bot.log · turn off: scripts/autostart.sh uninstall"
    ;;
  uninstall)
    unload
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
