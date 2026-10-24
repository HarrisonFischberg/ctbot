# Running CTbot on a cloud server

Goal: the paper bot keeps trading during market hours when the Mac is off (a flight, a dead
battery), and you get a phone alert within a few minutes if it stops. Paper only: nothing here
enables live trading (see the last section).

**Nothing in this repo deploys itself.** The files below only help once you rent a server and
run them. Keys and the Robinhood sign-in are never copied by a script: you put them in place.

| File | What it does |
|---|---|
| `deploy/setup-server.sh` | Ubuntu 24.04, run as root: packages, a `ctbot` user, firewall (SSH only), read-only deploy key, clone, venv, systemd units. Run it again to update. Starts the bot only once `.env` and the Robinhood sign-in exist |
| `deploy/ctbot-paper.service` | `ctbot-paper --loop --interval 300`, restarted if it stops; `LIVE_TRADING_ENABLED=false` and `CTBOT_MODE=paper` pinned (they win over `.env`) |
| `deploy/ctbot-dashboard.service` | the dashboard, still on `127.0.0.1:8787` only |
| `deploy/ctbot-watchdog.{service,timer}` | `ctbot-watchdog` every minute: pings `HEARTBEAT_URL` |
| `src/ctbot/watchdog.py` | the health check (works on the Mac too: `.venv/bin/ctbot-watchdog`) |

## Which host

The bot is one long-running Python process plus the dashboard, with files it must keep (the
paper ledger in `data/`, the sign-in in `~/.ctbot/`). So it needs an always-on machine with a
persistent disk, not a serverless or scale-to-zero platform. About 2 GB of RAM covers pandas,
the variant books and the daily Cboe download. Prices are approximate (late 2026); check them.

| Host | ~Cost/month | Reliability | Ease | Notes |
|---|---|---|---|---|
| **DigitalOcean droplet, 2 GB** (NYC) | ~$12, +20% for weekly backups | Good, simple SLA | Easiest UI; one-click snapshots | Plain Ubuntu VM: `setup-server.sh` runs as is |
| Hetzner Cloud (Ashburn VA) | ~$6-9 | Good | Fine; fewer US regions | Cheapest real VM; raised prices in April 2026 |
| AWS Lightsail, 2 GB | ~$12 | Very good | OK; AWS console is busier | Same as a droplet, AWS billing |
| Fly.io machine + volume | ~$8-12 | Good | Needs a Dockerfile and `fly` CLI | The browser sign-in and dashboard go through `fly proxy`; more moving parts |
| Render / Railway worker | ~$7-20 + disk | Good | Git-push deploys | Disks cost extra and redeploys restart the bot; awkward for the OAuth sign-in |
| Oracle Cloud free tier | $0 | Variable | Fiddly signup | Idle free VMs can be reclaimed; capacity often unavailable |
| GitHub Actions / cron services | $0 | n/a | n/a | Not suitable: jobs are short-lived, no persistent ledger |

**Recommendation: a 2 GB DigitalOcean droplet in NYC with weekly backups (~$15/month),
Tailscale for private access, and Healthchecks.io (free tier) for the heartbeat alert.**
It is a plain VM, so the same systemd setup works on Hetzner if you'd rather save ~$6/month.

## Alerts: how the heartbeat works

1. Create a free check at [healthchecks.io](https://healthchecks.io): period **1 minute**,
   grace **5 minutes**. Add the phone app or Pushover/Telegram/SMS/email integration.
2. Put its ping URL in the server's `.env` as `HEARTBEAT_URL=https://hc-ping.com/<uuid>`.
3. Every minute `ctbot-watchdog` checks, from files only:
   - the bot's heartbeat file is under 90 s old (the process is alive);
   - during market hours, a cycle finished within the last 3 intervals + 5 min (it is not
     failing every cycle, e.g. after an expired sign-in or a data outage);
   - the paper log's last lines don't say "Robinhood sign-in needed".

   Healthy: ping the URL. Unhealthy: ping `/fail` with the reason, which alerts at once.
4. If the server itself dies, the pings stop and Healthchecks alerts after the grace period.

The ping carries only the one-line reason (for example "cycles not finishing: last completed
cycle 31 min ago"), never a key, a balance or a position.

## Setting it up (each step needs your go-ahead; nothing here has been done yet)

1. **Create the droplet**: Ubuntu 24.04, 2 GB, NYC, your SSH key (no password login), backups on.
2. **Run the setup**: `scp deploy/setup-server.sh root@<ip>:` then `ssh root@<ip> bash setup-server.sh`.
   It prints a deploy key: add it under the repo's *Settings > Deploy keys* (read-only),
   then run the script again. It clones, installs and stops, saying what is still needed.
3. **Private access (Tailscale)**: on the server `curl -fsSL https://tailscale.com/install.sh | sh && tailscale up --ssh`,
   and install Tailscale on the Mac and phone. Then close public SSH:
   `ufw allow in on tailscale0 && ufw delete allow OpenSSH`. From here on use `ssh ctbot@<tailscale-name>`.
4. **Secrets (`.env`)**: create `/home/ctbot/ctbot/.env` from `.env.example` with the same values
   as the Mac (Anthropic key, risk settings, `MARKET_DATA=robinhood`, `ENTRY_MODE`, ...) plus
   `HEARTBEAT_URL`. Paste it over SSH into an editor rather than copying the Mac file, so no stray
   backup copy is left; then `chmod 600` and `chown ctbot:ctbot`. Keys saved from the dashboard's
   Settings tab live in `~/.ctbot/settings.json` and can be re-entered there instead.
5. **Robinhood sign-in on the server** (no token copy needed): the sign-in calls back to
   `localhost:8765`, so forward that port from the Mac:
   ```
   ssh -L 8765:localhost:8765 ctbot@<tailscale-name>
   cd ctbot && .venv/bin/ctbot-robinhood-login
   ```
   Open the URL it prints in the Mac's browser and sign in; the redirect reaches the server
   through the tunnel and the token is saved at `/home/ctbot/.ctbot/robinhood_oauth.json` (0600).
   It refreshes itself; if Robinhood ever revokes it, the watchdog alerts with "sign-in needed"
   and you repeat this step.
6. **Move the paper books (optional)**: to keep the Mac's history, stop the Mac bot
   (`scripts/autostart.sh uninstall`) and copy `data/` and `config/strategies.toml` to the server
   before starting. Don't run both bots afterwards: they'd keep two diverging ledgers.
7. **Start**: run `setup-server.sh` once more. It starts the bot and dashboard, enables the
   watchdog timer, and prints the first watchdog verdict.

### Daily use

- **Dashboard**: `ssh -L 8787:127.0.0.1:8787 ctbot@<tailscale-name>`, then open
  http://127.0.0.1:8787 on the Mac. Nothing listens publicly; the dashboard is unchanged.
- **Logs**: `journalctl -u ctbot-paper -f` (also `logs/ctbot-paper.log`).
- **Update to the latest main**: `sudo bash ~ctbot/ctbot/deploy/setup-server.sh` (pulls, reinstalls,
  restarts). The ledger is on disk, so a restart keeps positions; prefer doing it outside market hours.
- **Pause new entries**: `touch ~ctbot/ctbot/data/HALT` (exits still run), as on the Mac.

## Security notes

- The Robinhood token belongs to the agentic account, which Robinhood may let place orders.
  The client still refuses every tool outside its read-only allowlist, but a stolen token is
  the main risk of hosting: SSH keys only, no public ports after step 3, the bot runs as an
  unprivileged `ctbot` user, and the token and `.env` are mode 0600.
- The deploy key is read-only, so a compromised server can't push to the repo.
- Unattended security upgrades are installed by the setup script.

## Live trading

The server runs `ctbot-paper` only: the units pin `LIVE_TRADING_ENABLED=false` and there is no
`ctbot-live` unit. Live execution (`ctbot.live`) is not implemented yet, so "enable live later"
would be a separate change, reviewed on its own, needing the three live flags, its own process
and its own unit. Moving the bot to a server doesn't make that step any closer or easier by
accident.
