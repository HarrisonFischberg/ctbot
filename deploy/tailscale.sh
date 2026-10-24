#!/bin/bash
# Put the server on your Tailscale network and serve the dashboard there (tailnet only). As root:
#   bash tailscale.sh                     install, sign in (prints a link), serve the dashboard
#   bash tailscale.sh --close-public-ssh  afterwards, over Tailscale: SSH only via the tailnet
# Never `tailscale funnel`: that would publish the dashboard to the internet.
set -euo pipefail
{
ENV=/home/ctbot/ctbot/.env
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi

if [ "${1:-}" = "--close-public-ssh" ]; then
  server_ip=$(echo "${SSH_CONNECTION:-}" | awk '{print $3}')  # the address this SSH session came in on
  case "$server_ip" in 100.*|fd7a:*) ;; *)
    echo "Run this over Tailscale (ssh root@<tailscale name>), so that path is proven first." >&2
    exit 1 ;;
  esac
  ufw allow in on tailscale0 to any port 22 proto tcp >/dev/null
  ufw delete allow OpenSSH >/dev/null 2>&1 || true
  ufw status verbose
  echo "Public SSH closed. Recovery if Tailscale ever breaks: Hetzner web console."
  exit 0
fi

command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=ctbot  # prints a sign-in link the first time; approve it in your browser
tailscale serve --bg 8787      # HTTPS on the tailnet -> 127.0.0.1:8787 (may ask to enable HTTPS once)
name=$(tailscale status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
if grep -q '^CTBOT_DASHBOARD_HOSTS=' "$ENV"; then
  sed -i "s|^CTBOT_DASHBOARD_HOSTS=.*|CTBOT_DASHBOARD_HOSTS=$name|" "$ENV"
else
  echo "CTBOT_DASHBOARD_HOSTS=$name" >> "$ENV"
fi
chown ctbot:ctbot "$ENV" && chmod 600 "$ENV"
systemctl restart ctbot-dashboard
echo
echo "Dashboard: https://$name  (any device signed in to your Tailscale)"
echo "Next, from the Mac: ssh root@$name 'bash /home/ctbot/ctbot/deploy/tailscale.sh --close-public-ssh'"
exit 0
}
