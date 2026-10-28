# CTbot · Creature Trading Bot

A paper-trading bot you run on your own computer or a small server. Turn on **Autopilot**, give it
one amount of paper money and three rules, and it picks the strategies, funds them and trades
them for you. CTbot reads live prices from your own Robinhood
account, can ask Claude to review each trade idea, and runs every order through hard risk rules
first.

> **Paper trading only, today.** Every trade is simulated. CTbot reads from Robinhood but never
> sends it an order: the real-money code is unfinished and disabled. Read the
> [risk notice](DISCLAIMER.md) before you use it, with paper or real accounts. Not investment
> advice. All risk is yours.

Free and open source ([MIT](LICENSE)) by [Rare Creatures](https://rarecreatures.app). No accounts,
no servers of ours, no tracking: your keys and data stay on your machine (or on a server you rent
yourself, see [Run it on a server](#run-it-on-a-server-recommended-for-all-day-trading)).

**New: Opening Bell.** The dashboard has a new default look: light and blue, a live ticker tape,
big numbers and a left rail (bottom bar on a phone), in light or dark. The older design is still a
click away in the style menu.

<p align="center">
  <img src="docs/images/home.png" alt="CTbot Home in Opening Bell: portfolio value $10,107.59 with today's gain, drawdown and gain since starting, a value chart, account allocation, Bub's take, holdings and running strategies" width="800">
</p>

### Autopilot: the bot picks, funds and trades the strategies for you

| Set three rules, flip it on, give it an amount | Every strategy it may use, and why |
|---|---|
| <img src="docs/images/autopilot.png" alt="Autopilot tab: trading rules (max loss per trade 5%, give back from high 8%, how sure Claude must be 20%), Invest for Me with $10,500, the strategy vs Nasdaq backtest chart, latest decisions, running strategies and the strategy library" width="400"> | <img src="docs/images/library.png" alt="Strategy library: 28 strategies trading and 12 benched, each with the reason, such as no practice trades lately or failed its test because results could be luck" width="400"> |
| **Activity: every idea, what happened and why** | **Dark mode** |
| <img src="docs/images/activity.png" alt="Activity tab: the bot's trade ideas with what happened to each and what Claude said, then results by strategy" width="400"> | <img src="docs/images/home-dark.png" alt="Home in Opening Bell dark mode" width="400"> |

<sub>Screenshots: Opening Bell with made-up demo data (generated prices and a paper account; no real account). Every number comes from those made-up prices, so none of it is a real result or a promise of anything.</sub>

**Want it running all day like mine, with phone alerts?** Follow
**[Set up CTbot exactly like mine](docs/SETUP.md)**: a ~$7/month server, a private dashboard on
your Mac and phone over Tailscale, ntfy phone messages, and Autopilot investing for you, step by
step.

## What you get

- **A dashboard** at `http://127.0.0.1:8787` (or privately on your phone, see
  [SETUP.md](docs/SETUP.md)), in the Opening Bell style by default, light or dark:
  - **Home**: portfolio value and its chart, account allocation, Bub's take on the market, a
    4-step setup checklist, holdings (with each stop), running strategies and a stock chart.
  - **Autopilot**: switch it on and set three rules: max loss per trade, how much of a run-up to
    give back before selling, and how sure Claude must be before a buy. CTbot then picks trades
    from every strategy by itself, with trailing stops and no fixed profit cap. **Invest for Me**
    takes one dollar amount, picks and funds the strategies itself, re-picks weekly and tops them
    up as cash frees. It learns from its own paper record: each closed trade gets a post-mortem
    (`data/trade_log.jsonl`), a strategy's size grows or shrinks with its last 30 days of results,
    a strategy losing over 10+ trades is benched, and the account stops borrowing after a bad day,
    a drawdown or a QQQ downtrend. Its sections: *Overview* (rules, decisions, what's running),
    *Strategy library* (every strategy, trading or benched and why), *Testing* (crash test back to
    1999, a no-hindsight walk-forward test, the comparison books), *Backtests* (growth of $5,000
    since 2010, the leaderboard) and *Outside traders*.
  - **Activity**: the bot's trade ideas and what happened to each, results by strategy, what each
    trade taught, safety limits, trade history, the watchlist and a live log.
  - **Settings**: add your API keys, connect Robinhood, run a health check, review Bub's changes.
- **Strategies** you can run alone or together: Nasdaq-100 trend (plain and 2× leveraged), buy the
  dip on index funds, turn of the month, and same-day breakout models. Each is described, with its
  risk level, on the Autopilot tab.
- **Hard limits** no model can override: per-position size, total money, daily loss pause,
  drawdown stop, bid/ask spread and liquidity checks, no holding through earnings, SEC 8-K red
  flags, and your own max loss and take-profit per strategy. Optional rules from the trading skill
  packs (`.claude/skills/`): risk-per-trade sizing, total open risk, entries per day, minimum
  reward/risk, half off at 1R, a VWAP entry gate and a bear-market gate (see `.env.example`).

## Quick start

You need macOS (tested) or Linux (not tested yet), Python 3.11 or newer, a Robinhood account with agent (MCP) access, and
optionally an [Anthropic API key](https://console.anthropic.com/) for Claude reviews.

```bash
git clone https://github.com/HarrisonFischberg/ctbot.git
cd ctbot
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env              # optional: you can also add keys in the dashboard
.venv/bin/pytest -q               # every test should pass
./run_ctbot.sh                    # dashboard + paper bot; Ctrl+C stops both
```

Open `http://127.0.0.1:8787`, go to **Settings**, add your keys and press **Connect Robinhood**.
Then open **Autopilot**, set your rules, flip it on and press **Invest for Me** (or invest in one
strategy from the library). The bot trades during US market hours while
`./run_ctbot.sh` is running.

### Run it on a server (recommended for all-day trading)

A laptop sleeps, travels and loses Wi-Fi. For a bot that trades every session, run it on a small
Linux server instead (a ~$7/month Hetzner or ~$12 DigitalOcean VM works). Step by step:
[docs/SETUP.md](docs/SETUP.md). Every file and timer: [docs/CLOUD_HOSTING.md](docs/CLOUD_HOSTING.md). In short:

- `deploy/setup-server.sh` (Ubuntu 24.04, as root) installs CTbot as systemd services, paper only.
- `deploy/tailscale.sh` puts the dashboard on your private Tailscale network
  (`https://ctbot.<tailnet>.ts.net` on your Mac and phone) and can close public SSH.
- **Updates deploy themselves**: merge to `main` and `ctbot-update.timer` pulls it and restarts
  the bot at 08:45 or 16:20 ET on the next weekday, never while the market is open.
- **Alerts**: `ctbot-watchdog` pings a Healthchecks.io URL every minute (`HEARTBEAT_URL`) and,
  with `NOTIFY_URL` set to an ntfy topic, sends phone messages at the open, after the close and
  when something breaks.

Run only one bot per paper account: once the server trades, turn Mac autostart off
(`scripts/autostart.sh uninstall`).

### What runs, and when

| What | When | Where it lives |
|---|---|---|
| Paper bot, main book | every 5 minutes in market hours; stops checked every 30 s | `ctbot-paper`, `data/` |
| Comparison books (one setting changed each) | right after each main cycle, same data and Claude verdicts | `config/variants.toml`, `data/variants/<name>/` |
| Dashboard | always | `ctbot-dashboard`, `127.0.0.1:8787` |
| Watchdog: heartbeat ping, phone messages | every minute | `ctbot-watchdog` |
| Research refresh: bar caches, idea scoring, preflight, day plan, reconciliation | weekdays 08:15 and 16:30 ET | `scripts/c2_track_weekly.sh` |
| Auto-update from `main` (server only) | weekdays 08:45 and 16:20 ET | `deploy/update.sh` |

The comparison books are how new rules get tried: each starts with $10,000 of paper money and
differs from `base` in one thing (trail, starting stop, Claude's confidence floor, position count,
ideas per cycle, the reviewing model, or the trading-skill rules). Compare them on the Autopilot
page's *Settings test* card or with `.venv/bin/ctbot-variants`. Nothing promotes a book's settings
to the main book by itself.

### Start at login (macOS, optional)

Not needed if the bot runs on a server (and don't run both). To have the dashboard and paper bot start when you log in and come back if they stop:

```bash
scripts/autostart.sh install      # start now, at every login, and after a crash
scripts/autostart.sh status       # running? pid and last exit code
scripts/autostart.sh uninstall    # stop it and turn autostart off
```

Keys must be in the dashboard's **Settings** tab or in `.env`: launchd doesn't read your shell
profile, so a key you only `export` there is invisible to the agent (and an old one in `.env`
gets used instead). The health check names where its Claude key came from when it fails.

It runs the same `./run_ctbot.sh`, paper only (the agent pins `LIVE_TRADING_ENABLED=false`),
without opening a browser tab; output goes to `logs/bot.log`. Stop any `./run_ctbot.sh` you
started by hand before installing, and don't start one while autostart is on, or two bots
would trade the same paper account.

Keeping the Mac awake: the bot doesn't do this itself. Use an app that keeps the Mac awake
during market hours (or macOS Energy settings). Closing a laptop's lid still sleeps it unless it
has power and an external display (clamshell mode).

More commands, including backtests and the research lab: [docs/COMMANDS.md](docs/COMMANDS.md).
How it fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Assistant

**Bub**, the little creature at the bottom of the left rail (bottom bar on a phone), answers questions about what
the paper bot is doing and why ("Why so few trades today?", "Anything broken?"), in plain language
with the bot's own numbers, and suggests changes when you ask for them ("Take more trades").

<p align="center"><img src="docs/images/assistant.png" alt="Bub's chat panel: why only 2 trades were taken today, and a card proposing Min Claude confidence 0.65 to 0.55 with its reason, risk, and Apply and Dismiss buttons" width="360"></p>

- **What it reads**: the same snapshot the dashboard shows, plus all of today's trade ideas
  (`data/ideas.jsonl`) and today's paper log, counted by outcome and reason, and which tab, stock
  or idea you're looking at. Never your keys: `.env` and `~/.ctbot/` are not read into it. Your
  question and that summary go to Anthropic with your key, as trade reviews do.
- **What it can change**, each within bounds written in `src/ctbot/tuning.py` and
  `src/ctbot/assistant/actions.py`: the risk and ensemble knobs (`MIN_CLAUDE_CONFIDENCE`,
  `MIN_LONG_SCORE`, `MIN_AGREEING_STRATEGIES`, `MAX_CANDIDATES_PER_CYCLE`, `MAX_OPEN_POSITIONS`,
  the review budgets, `MAX_SPREAD_PCT`, `NO_ENTRIES_LAST_MINUTES`, `MAX_POSITION_PCT`,
  `ENTRY_MODE`), a strategy's shadow/trade mode, investing in or stopping a strategy, and paper
  orders, including approving a recommended idea.
- **Every change needs your click.** Bub only proposes; each suggestion is a card with the
  change, why, and the risk. Nothing happens until you press **Apply**, and the server checks it
  again then. Knob changes go to `~/.ctbot/tuning.json` and reach the running paper bot within
  seconds, no restart.
- **Everything is logged** in `data/assistant_audit.jsonl`: what you asked, what was proposed, the
  value before and after, and whether you applied or dismissed it. Settings → *Assistant
  changes* lists them, each with a one-click **Revert** (orders can't be reverted).
- **Paper only.** No action can turn on live trading, touch the Robinhood account settings, raise
  `MAX_TRADING_CAPITAL`, loosen the loss or stop limits, or allow shorting. It has no Robinhood
  access at all.
- It uses the Anthropic key from the Settings tab, with its own daily cap
  (`MAX_ASSISTANT_MESSAGES_PER_DAY`, default 100) that doesn't use up the trade-review budget.

## What it costs

CTbot is free. Services you connect may charge you:

- **Anthropic API**: billed to your key for each Claude review and each assistant message. The
  bot caps reviews per day (`MAX_CLAUDE_REVIEWS_PER_DAY`) and assistant messages separately
  (`MAX_ASSISTANT_MESSAGES_PER_DAY`). On a small account the review cost can exceed the edge it is
  checking. Strategies that follow a fixed rule never call Claude.
- **Robinhood**: its own fees and rules apply to your account.
- **Collective2** (optional): research data; some strategies there are paid subscriptions.

## Your data

Nothing is sent to us. Keys stay in `.env` or `~/.ctbot` (readable only by your user). Data goes
only from your computer to the services you connect. The dashboard sets no cookies. Details,
and how to delete everything: [PRIVACY.md](PRIVACY.md).

## Safety design

- Paper and real-money code run as separate programs. A paper process cannot even import the
  real-money module, and tests fail if anything else names an order-placing tool.
- Real money would need three separate settings to agree, plus an exact confirmation phrase, and
  would refuse any account except the one you name by full number and nickname. That code is not
  finished and is not enabled.
- The dashboard listens only on your computer and rejects requests from other websites.

## Disclaimer

CTbot is provided "as is", without warranty of any kind. It is not investment, financial, legal
or tax advice, and Rare Creatures LLC is not a broker or investment adviser. Trading involves
risk of loss, including all of the money you invest. Simulated and past performance do not
predict future results. AI reviews can be wrong.

While you can connect both paper and real brokerage accounts through Robinhood and MCP, and
anyone can modify this open-source code, **all risk from using it is yours**: every trade,
setting, change and loss. You must be 18 or older, and you are responsible for following the
terms of every service you connect and the laws that apply to you.

CTbot is not affiliated with, endorsed by or sponsored by Robinhood Markets, Inc., Anthropic or
Collective2. Their names are used only to describe what CTbot connects to. Full text:
[DISCLAIMER.md](DISCLAIMER.md) · [TERMS.md](TERMS.md) · [PRIVACY.md](PRIVACY.md) ·
[SECURITY.md](SECURITY.md).

## License and contact

[MIT](LICENSE) © 2026 Rare Creatures LLC, Colorado, United States · hello@rarecreatures.app ·
Issues and pull requests welcome.
