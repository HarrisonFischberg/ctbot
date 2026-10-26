#!/bin/bash
# Set up (or update) CTbot on a fresh Ubuntu 24.04 server. Run as root:
#   bash setup-server.sh            first run: packages, ctbot user, clone, venv, units
#   bash setup-server.sh            again any time to update
# CTBOT_REPO=git@github.com:<you>/<fork>.git uses a read-only deploy key instead (private forks).
# It never copies keys or the Robinhood sign-in: you put .env in place and sign in yourself
# (docs/CLOUD_HOSTING.md). It starts the bot only once both exist. Paper only.
set -euo pipefail
{  # read to the closing brace before running: the git pull below may rewrite this file
REPO=${CTBOT_REPO:-https://github.com/HarrisonFischberg/ctbot.git}  # public: no key needed
BRANCH=${CTBOT_BRANCH:-main}
USER_NAME=ctbot
HOME_DIR=/home/$USER_NAME
APP=$HOME_DIR/ctbot
UNITS="ctbot-paper.service ctbot-dashboard.service ctbot-watchdog.service ctbot-watchdog.timer ctbot-update.service ctbot-update.timer ctbot-research.service ctbot-research.timer"
as_ctbot() { sudo -u "$USER_NAME" -H "$@"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo bash $0)." >&2
  exit 1
fi

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq git python3-venv python3-pip poppler-utils ufw unattended-upgrades

id "$USER_NAME" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh" "$HOME_DIR/.ctbot"

# Firewall, first run only (later runs keep your rules, e.g. SSH over Tailscale only): SSH in,
# nothing else. The dashboard listens on loopback and is reached through an SSH tunnel.
if ! ufw status | grep -q "Status: active"; then
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null
  ufw --force enable >/dev/null
fi

if [ ! -d "$APP/.git" ] && [[ "$REPO" == git@* ]]; then
  KEY=$HOME_DIR/.ssh/ctbot_deploy
  if [ ! -f "$KEY" ]; then
    as_ctbot ssh-keygen -q -t ed25519 -N "" -C "ctbot-server-deploy" -f "$KEY"
    as_ctbot sh -c "printf 'Host github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' '$KEY' >> '$HOME_DIR/.ssh/config'"
  fi
  hello=$(as_ctbot ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true)  # exits 1 even on success
  if ! grep -q "successfully authenticated" <<<"$hello"; then
    echo
    echo "Add this public key to the repo as a read-only deploy key"
    echo "(GitHub -> repo Settings -> Deploy keys -> Add deploy key, leave write access off):"
    echo
    cat "$KEY.pub"
    echo
    echo "Then run this script again."
    exit 0
  fi
fi
if [ ! -d "$APP/.git" ]; then
  as_ctbot git clone --branch "$BRANCH" "$REPO" "$APP"
else
  as_ctbot git -C "$APP" pull --ff-only origin "$BRANCH"
fi

as_ctbot mkdir -p "$APP/logs" "$APP/data"
[ -x "$APP/.venv/bin/python" ] || as_ctbot python3 -m venv "$APP/.venv"
as_ctbot "$APP/.venv/bin/pip" install -q --upgrade pip
as_ctbot "$APP/.venv/bin/pip" install -q -e "$APP"

for u in $UNITS; do install -m 644 "$APP/deploy/$u" /etc/systemd/system/; done
systemctl daemon-reload
systemctl enable ctbot-paper.service ctbot-dashboard.service ctbot-watchdog.timer ctbot-update.timer ctbot-research.timer >/dev/null

missing=""
[ -f "$APP/.env" ] || missing="$missing\n  - $APP/.env (copy .env.example, fill it in, chmod 600, chown $USER_NAME)"
[ -f "$HOME_DIR/.ctbot/robinhood_oauth.json" ] || missing="$missing\n  - Robinhood sign-in (docs/CLOUD_HOSTING.md, step 5)"
if [ -n "$missing" ]; then
  printf "Installed. Not starting the bot yet; still needed:%b\n" "$missing"
  exit 0
fi
chmod 600 "$APP/.env"
if grep -Eiq '^[[:space:]]*LIVE_TRADING_ENABLED[[:space:]]*=[[:space:]]*"?true' "$APP/.env"; then
  echo "Note: .env turns live trading on; the units pin it off and run paper only." >&2
fi
systemctl restart ctbot-paper.service ctbot-dashboard.service
systemctl start ctbot-watchdog.timer ctbot-update.timer ctbot-research.timer
sleep 10
systemctl --no-pager --lines=0 status ctbot-paper.service ctbot-dashboard.service || true
as_ctbot sh -c "cd '$APP' && .venv/bin/ctbot-watchdog" || true
exit 0
}
