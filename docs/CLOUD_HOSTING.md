# Running CTbot on a cloud server

Goal: the paper bot keeps trading during market hours when the Mac is off (a flight, a dead
battery), and you get a phone alert within a few minutes if it stops. Paper only: nothing here
enables live trading (see the last section).

**Status (2026-10-26): this is how CTbot runs.** The paper bot lives on a Hetzner CX23 (Ubuntu
24.04) set up with the files below, reached over Tailscale only, with Healthchecks.io and ntfy
alerts. The Mac no longer runs the bot (its autostart is uninstalled), so don't reinstall it.

Renting the server is up to you, and keys and the Robinhood sign-in are never copied by a
script: you put them in place. After that first setup, merges to `main` deploy themselves.

| File | What it does |
|---|---|
| `deploy/setup-server.sh` | Ubuntu 24.04, run as root: packages, a `ctbot` user, firewall (SSH only), read-only deploy key, clone, venv, systemd units. Run it again to update. Starts the bot only once `.env` and the Robinhood sign-in exist |
| `deploy/ctbot-paper.service` | `ctbot-paper --loop --interval 300`, restarted if it stops; `LIVE_TRADING_ENABLED=false` and `CTBOT_MODE=paper` pinned (they win over `.env`) |
| `deploy/ctbot-dashboard.service` | the dashboard, still on `127.0.0.1:8787` only |
| `deploy/ctbot-watchdog.{service,timer}` | `ctbot-watchdog` every minute: pings `HEARTBEAT_URL`, sends `NOTIFY_URL` phone messages |
| `deploy/ctbot-research.{service,timer}` | weekdays 08:15 and 16:30 ET: `scripts/c2_track_weekly.sh` (bar caches, idea scoring, preflight, premarket plan, end-of-day reconciliation), the server's copy of the Mac's launchd job |
| `deploy/update.sh`, `deploy/ctbot-update.{service,timer}` | weekdays 08:45 and 16:20 ET: if main changed, pull and rerun the setup (restarts the bot); never while the market is open |
| `deploy/tailscale.sh` | as root: install Tailscale, sign in, `tailscale serve` the dashboard, set `CTBOT_DASHBOARD_HOSTS`; `--close-public-ssh` afterwards |
| `src/ctbot/watchdog.py`, `src/ctbot/notify.py` | the health check and phone messages (the check works on the Mac too: `.venv/bin/ctbot-watchdog`) |

## What runs, and when (server times are America/New_York)

| Unit | Schedule | Does |
|---|---|---|
| `ctbot-paper` | always on; 5-minute cycles in market hours, stops every 30 s | the main book, then every book in `config/variants.toml` in the same process (shared data and Claude verdicts) |
| `ctbot-dashboard` | always on | dashboard on 127.0.0.1:8787, served on the tailnet by `tailscale serve` |
| `ctbot-watchdog.timer` | every minute | heartbeat ping; open/close/health phone messages |
| `ctbot-research.timer` | Mon-Fri 08:15, 16:30 | `scripts/c2_track_weekly.sh`: bar caches, idea scoring, performance figures, preflight, premarket plan, reconciliation |
| `ctbot-update.timer` | Mon-Fri 08:45, 16:20 | `deploy/update.sh`: pull `main` if it changed and rerun the setup; skipped while the market is open |

`systemctl list-timers 'ctbot-*'` shows the next runs; `journalctl -u <unit>` shows what each did.

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
CTbot's own server is a Hetzner CX23 (~$7/month); it has run this setup since 2026-10-24.

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

## Phone messages (optional)

Set `NOTIFY_URL=https://ntfy.sh/<long-random-topic>` in the server's `.env` and subscribe to the
same topic in the free ntfy app. The watchdog then sends "Market open: CTbot is trading" after the
first cycle of each session (with the premarket plan's verdict), a summary 15 minutes after the
close (paper equity change, fills), and a message when the bot turns unhealthy or recovers.
Healthchecks still covers the server itself going down.

## Setting it up (each step needs your go-ahead)

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
   watchdog, research and update timers, and prints the first watchdog verdict.
8. **Phone messages (optional)**: add `NOTIFY_URL` to `.env` (see *Phone messages* above); the
   watchdog picks it up at its next run, no restart.

### Dashboard over Tailscale (no Terminal)

`deploy/tailscale.sh` (as root) does all of this: install, sign in, serve, set the name in `.env`;
then, over Tailscale, `tailscale.sh --close-public-ssh` closes public SSH. By hand: on the server, after step 3: `tailscale serve --bg 8787` (HTTPS on the tailnet, proxied to the
loopback dashboard), then add the server's MagicDNS name to `.env` as
`CTBOT_DASHBOARD_HOSTS=ctbot.<tailnet>.ts.net` and `systemctl restart ctbot-dashboard`.
Open `https://ctbot.<tailnet>.ts.net` on any device signed in to your tailnet (Mac, phone).
Only `*.ts.net` names are accepted, the server still listens on 127.0.0.1 only, and every click
still needs the page's token and a same-origin request. Never use `tailscale funnel`: it
publishes to the internet.

### Daily use

- **Dashboard**: the Tailscale address above, or `ssh -N -L 8787:127.0.0.1:8787 ctbot@<server>`
  and http://127.0.0.1:8787. Nothing listens publicly.
- **SSH**: once public SSH is closed, only over Tailscale (`ssh root@ctbot.<tailnet>.ts.net`, with
  Tailscale on). If Tailscale is down, use the host's web console.
- **Logs**: `journalctl -u ctbot-paper -f` (also `logs/ctbot-paper.log`); research refresh in
  `logs/c2-track.log`.
- **Health now**: `journalctl -u ctbot-watchdog -n 5` (the last few verdicts).
- **Updates**: automatic. Merge to main and `ctbot-update.timer` deploys it at the next 08:45 or
  16:20 ET weekday run (`journalctl -u ctbot-update` shows what it did). To deploy now:
  `sudo bash ~ctbot/ctbot/deploy/update.sh` (it refuses while the market is open; the setup script
  itself doesn't, so use it during market hours only on purpose). Don't edit tracked files on the
  server: the pull is fast-forward only and stops on local changes.
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
