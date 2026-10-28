# Set up CTbot exactly like mine

This is the whole setup CTbot's author runs every day, start to finish: a small cloud server that
trades paper money all day, a private dashboard on your Mac and phone, phone alerts through ntfy,
and Autopilot choosing and funding the strategies. Budget about an hour, most of it waiting for
installs. **Paper money only**: nothing here can place a real order.

If you only want to try it on your laptop first, the [Quick start](../README.md#quick-start) in the
README takes five minutes. Come back here when you want it running while your laptop is closed.

## What you need

| What | Why | Cost |
|---|---|---|
| A GitHub account | to fork the repo, so your server updates from your own copy | free |
| A Robinhood account with agent (MCP) access | live prices and charts (read-only) | free |
| An [Anthropic API key](https://console.anthropic.com/) | Claude double-checks trade ideas, and powers Bub | pay per use, capped (see step 5) |
| A small Linux server ([Hetzner](https://www.hetzner.com/cloud) CX23 or a 2 GB [DigitalOcean](https://www.digitalocean.com/) droplet) | runs the bot when your computer is off | ~$7-15/month |
| [Tailscale](https://tailscale.com/) on the server, your computer and your phone | opens the dashboard privately, nothing on the public internet | free |
| [Healthchecks.io](https://healthchecks.io/) | tells you if the server stops | free |
| The [ntfy](https://ntfy.sh/) app on your phone | "market open", the day's summary, and problems | free |
| Optional: an OpenAI API key | Bub's backup if Claude errors | pay per use |

Never paste a key into a GitHub issue, a commit or a screenshot. Keys live only in the server's
`.env` file or in the dashboard's Settings tab (saved in `~/.ctbot/`, readable only by you).

## 1. Fork the repo

On GitHub, press **Fork** on [HarrisonFischberg/ctbot](https://github.com/HarrisonFischberg/ctbot).
Your server will track your fork's `main`, so updates reach it only when you choose to sync.

## 2. Rent the server

Create an **Ubuntu 24.04** server with **2 GB of RAM** (Hetzner CX23 or a 2 GB droplet), add your
computer's SSH key, and turn password login off. Note its IP address. Leave everything else at
the defaults.

## 3. Install CTbot on it

From your computer (replace `<you>` with your GitHub name and `<ip>` with the server's address):

```bash
ssh root@<ip>
curl -fsSLO https://raw.githubusercontent.com/<you>/ctbot/main/deploy/setup-server.sh
CTBOT_REPO=https://github.com/<you>/ctbot.git bash setup-server.sh
```

It installs packages, makes a `ctbot` user, turns on a firewall (SSH only), clones your fork,
installs the systemd services and stops with a list of what is still missing: `.env` and the
Robinhood sign-in. That is expected.

## 4. Turn on alerts (Healthchecks and ntfy)

**Healthchecks** (is the server alive?): create a free check at
[healthchecks.io](https://healthchecks.io), period **1 minute**, grace **5 minutes**, and add its
phone app or an email integration. Copy its ping URL (`https://hc-ping.com/...`).

**ntfy** (what is the bot doing?): make up a long random topic name, since anyone who knows it can
read it:

```bash
echo "ctbot-$(openssl rand -hex 12)"
```

In the ntfy phone app, press **+** and subscribe to that topic on `ntfy.sh`. Keep the name to
yourself. You will get:

- **Market open**: "CTbot is trading", with the premarket plan's verdict.
- **After the close**: the day's paper P&L and fills.
- **Problems**: the moment the bot turns unhealthy (a failed sign-in, cycles not finishing), and
  again when it recovers.

A longer readout of every paper book is also posted each evening to `<topic>-readout`. Your phone
doesn't need to subscribe to it; it is there for a Claude session (or you) to read.

## 5. Write the server's `.env`

Still on the server (as root), make it from the example and open it:

```bash
cd /home/ctbot/ctbot && sudo -u ctbot cp .env.example .env && chmod 600 .env && nano .env
```

Fill in your own values for these lines, and leave the rest of the file as it is:

```bash
ANTHROPIC_API_KEY=<your key>
OPENAI_API_KEY=<optional, Bub's backup>
ROBINHOOD_ACCOUNT_NUMBER=<full number of the account to read>
ROBINHOOD_ACCOUNT_NICKNAME=<its nickname in the Robinhood app>
HEARTBEAT_URL=https://hc-ping.com/<your check>
NOTIFY_URL=https://ntfy.sh/<your topic from step 4>
```

Then the settings that make it trade the way mine does. They are all paper-only knobs, tuned to
find out quickly what works rather than to protect real money:

```bash
MARKET_DATA=robinhood          # live prices instead of the bundled CSVs
ENTRY_MODE=auto                # buys go through without waiting for a click
STARTING_CAPITAL=20000         # paper money to start with
MAX_TRADING_CAPITAL=20000      # and the most the bot may use
MAX_OPEN_POSITIONS=10
MIN_AGREEING_STRATEGIES=1
MAX_CANDIDATES_PER_CYCLE=10
MAX_CLAUDE_REVIEWS_PER_DAY=200
MAX_REVIEWS_PER_MODEL_PER_DAY=60
MAX_CLAUDE_USD_PER_DAY=3.00    # real money: the most Claude reviews may cost per day
MAX_DAILY_LOSS_PCT=0.10
MAX_TOTAL_DRAWDOWN_PCT=0.40
```

`MAX_CLAUDE_USD_PER_DAY` is the one line that spends real money, on your Anthropic bill. Each
comparison book in `config/variants.toml` has its own cap, so a day with every book busy costs a
few times this (mine has been about $2 a day in total). Lower it (or switch `CLAUDE_MODEL` to Haiku) to spend
less. `LIVE_TRADING_ENABLED` stays `false`, and the server's services pin it off anyway.

## 6. Make the dashboard private with Tailscale

Install Tailscale on your computer and phone and sign in to both with the same account. Then on
the server:

```bash
bash /home/ctbot/ctbot/deploy/tailscale.sh
```

It signs the server in to your tailnet (open the link it prints), serves the dashboard at
`https://ctbot.<your-tailnet>.ts.net` and tells the dashboard to accept that name. Once
that name works from your computer, close public SSH (run it over Tailscale, as shown, so the
script knows that path works before it shuts the other):

```bash
ssh root@ctbot.<your-tailnet>.ts.net 'bash /home/ctbot/ctbot/deploy/tailscale.sh --close-public-ssh'
```

From now on the server has no open ports on the internet. If Tailscale is ever down, your host's
web console still gets you in.

## 7. Sign in to Robinhood from the server

Robinhood's sign-in calls back to `localhost:8765`, so forward that port from your computer:

```bash
ssh -L 8765:localhost:8765 root@ctbot.<your-tailnet>.ts.net
sudo -u ctbot -H bash -c 'cd ~/ctbot && .venv/bin/ctbot-robinhood-login'
```

Open the link it prints in your computer's browser and sign in. The token is saved on the server
only (`~/.ctbot/robinhood_oauth.json`, mode 600) and refreshes itself. CTbot uses only read-only
Robinhood tools: quotes, history, positions. If Robinhood ever signs it out, ntfy tells you and you
repeat this step.

## 8. Start it

```bash
sudo bash /home/ctbot/ctbot/deploy/setup-server.sh
```

This time it starts the bot, the dashboard and every timer, and prints the first health check. Open
`https://ctbot.<your-tailnet>.ts.net` on your computer or phone: you land on **Opening Bell**, the
default dashboard (the older look is in the style menu, top right).

## 9. Add keys for Bub (if you skipped them in `.env`)

**Settings** tab: paste the Anthropic key (Bub and trade reviews) and, optionally, the OpenAI key
(Bub's backup). Saved keys work at once and are never shown again. Press **Run check** under
*Check connections*: Robinhood, Claude and the heartbeat should all be green.

Bub is the creature button bottom left (bottom right on a phone). Ask it "why so few trades
today?" or "take more trades": it answers from the bot's own numbers and proposes changes as
cards. Nothing changes until you press **Apply**, every change is logged under *Assistant changes*
with a **Revert** button, and it can never turn on real money or raise your loss limits.

## 10. Turn on Autopilot

Open the **Autopilot** tab:

1. **Trading rules** (left column), then **Save Rules**. Mine:
   - **Max loss per trade: 5%**. Each position's starting stop.
   - **Give back from high: 8%**. Once a trade is up, the stop trails 8% below its best price,
     so winners keep running with no fixed profit cap.
   - **How sure Claude must be: 20%**. Lower means more trades; higher means fewer, surer ones.
2. Flip **Autopilot** on (top right). The bot now picks trades from every strategy in the
   library that passed its tests, sizes each one by that strategy's last 30 days of results, and
   benches any strategy that keeps losing.
3. **Invest for Me**: type the amount (mine is all of it, $20,000), choose the max loss for that
   amount and press **Invest for Me**. CTbot picks the strategies itself, funds them, re-picks
   weekly (only swapping a pick for one that scores 20% better) and tops them up as cash frees.
4. If the account has less than your limit (for example after losses), **Add paper cash** tops it
   back up. Deposits are recorded and kept out of your P&L.

That's it. **Pause All Strategies** stops new buys any time (exits still run).

## Keeping it running

- **Updates**: on weekdays at 08:45 and 16:20 ET the server pulls your fork's `main` and restarts,
  never while the market is open. To take new CTbot releases, press **Sync fork** on GitHub.
- **Health**: `journalctl -u ctbot-watchdog -n 5` on the server, or just trust ntfy and Healthchecks.
- **Logs**: `journalctl -u ctbot-paper -f`.
- **Stop all buying fast**: Pause All on the dashboard, or `touch ~ctbot/ctbot/data/HALT`.
- **Only one bot per paper account**: don't also run it on your laptop.

More detail on every file and timer: [CLOUD_HOSTING.md](CLOUD_HOSTING.md). All commands:
[COMMANDS.md](COMMANDS.md).

CTbot is not investment advice and all risk is yours; read the [risk notice](../DISCLAIMER.md).
