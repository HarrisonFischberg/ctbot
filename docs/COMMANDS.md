# CTbot commands

Run everything from the project folder:

```bash
cd path/to/ctbot
```

(Or `source .venv/bin/activate` once per terminal and drop the `.venv/bin/` prefix.)

## Trading day

| What | Command |
|---|---|
| Everything at once: dashboard + paper bot (Ctrl+C stops both) | `./run_ctbot.sh` |
| Paper bot only | `.venv/bin/ctbot-paper --loop --interval 300` |
| Dashboard only | `.venv/bin/ctbot-dashboard` then open http://127.0.0.1:8787 |
| Pre-open health check | `.venv/bin/ctbot-preflight` |
| Premarket plan: trade or no-trade today, and why | `.venv/bin/ctbot-dayplan` |
| Stop all new entries now (exits still run) / resume | `touch data/HALT` / `rm data/HALT` |
| Robinhood sign-in (when preflight says it expired) | `.venv/bin/ctbot-robinhood-login` |

Only one paper bot can run at a time (a lock refuses a second). Start it before 9:30 ET;
the scanner model needs it running from 9:36 ET, the trend core trades 15:30-15:44 ET.

You can leave it running. Outside the session (nights, weekends, NYSE holidays; 13:00 ET on
half days) it does no analysis and fetches no quotes: it records the closing equity, lets the
Mac sleep, and resumes at the next open. The dashboard stops polling too: while the market is
closed it shows the last saved prices with their time ("prices as of ..."), fetching once after
the close only if it wasn't running near the bell. A sleeping Mac doesn't wake itself, so schedule a
weekday wake before the open, e.g. 06:55 Mountain (covers the 07:00 refresh job too):

```bash
sudo pmset repeat wakeorpoweron MTWRF 06:55:00
```

## Dashboard: Settings and the strategy launcher

- **Settings tab**: paste API keys (Anthropic, Collective2) and the Robinhood account number and
  nickname. They are saved in `~/.ctbot/settings.json` (only your user can read it), override
  `.env`, and are never shown again or committed. **Connect Robinhood** opens the read-only
  sign-in in your browser; **Run check** tests every connection. Restart the bot after a change.
- **Home**: portfolio value, a 4-step *Get started* checklist, and *Your strategies* (what you
  invested, value now, total return, Stop).
- **Autopilot**: turn it on and set two numbers: *max loss per trade* (how far below the fill a
  new trade's starting stop may sit) and *give back from the high* (how far the trailing stop
  follows below each position's highest price). Saved in `data/autopilot.json`; the bot reads it
  every cycle. While on, every enabled strategy may trade on its own (day trades, overnight holds,
  swings, the trend cores), except ones research rejected (the ranker) or that lost money over 10+
  paper trades in the last 30 days (shown as benched, with the reason). Claude reviews each
  non-systematic entry and sets its starting stop; those positions have no fixed take-profit, and
  winners keep rolling past their usual exit time while they are up. The trend cores keep their own
  backtested exits. The page also lists the latest decisions with Claude's reasons, and below it the
  strategy library and research (the old Discover and Research tabs). Autopilot replaces Invest
  runs while it is on. Paper only.
- **Strategy library** (on Autopilot, formerly Discover): every strategy in plain words with its simulated return over the past 3, 6 or 12
  months next to the S&P 500. **Invest** asks for an amount, a max loss and a take-profit; that
  strategy then trades on its own with that much paper money (never more), and the bot sells and
  stops it when the max loss or take-profit is reached. Every other model only suggests ideas
  while anything is invested. **Stop** sells what it bought at the next market check. With
  nothing invested, `config/strategies.toml` decides what trades. Paper only.
- **Activity**: the bot's trade ideas, positions, order history, safety limits and log.
- **Bub** (bottom right, every tab): ask what the bot did and why, or what to change. It only
  proposes: each change is a card that applies when you press **Apply**, within bounds, paper only.
  Knob changes are saved in `~/.ctbot/tuning.json` and reach the running bot within seconds;
  strategy shadow/trade switches edit `config/strategies.toml` and also apply without a restart.
  Every proposal and outcome is in `data/assistant_audit.jsonl`; Settings → *Assistant changes*
  has a Revert for each. Uses the Settings-tab Anthropic key, capped by
  `MAX_ASSISTANT_MESSAGES_PER_DAY` (default 100). To undo everything at once, delete
  `~/.ctbot/tuning.json`.
- Discover's numbers come from `.venv/bin/ctbot-perf` (cached prices only; the scheduled job
  refreshes them twice a day).

## Collective2

| What | Command |
|---|---|
| Update forward tracking of the watchlist | `.venv/bin/ctbot-c2-track` |
| Re-screen all strategies and rebuild the watchlist | `.venv/bin/ctbot-c2-rank` |

## After the close

| What | Command |
|---|---|
| Cache the last 2 weeks of 5-minute bars | `.venv/bin/ctbot-fetch-bars --interval 5minute --since $(date -v-14d +%Y-%m-%d)` |
| Score today's reviewed ideas (approved vs rejected) | `.venv/bin/ctbot-score-ideas` |
| Paper book tearsheet (after ~5 sessions) | `.venv/bin/ctbot-tearsheet` |
| Reconcile ledger, brackets and journal; P&L per model today | `.venv/bin/ctbot-reconcile` |

## Research

| What | Command |
|---|---|
| Strategy lab: leaderboard, Deflated Sharpe, walk-forward, crash stress test | `.venv/bin/ctbot-lab --cash-apy 0.035` |
| Trend length sweep (chosen on 2010-19, checked on 2020-26) | `.venv/bin/ctbot-sweep` |
| Tearsheet for a lab strategy | `.venv/bin/ctbot-tearsheet --lab "lev: trend QQQ 170d -> QLD capped at 30% vol"` |
| Intraday backtest (5-minute data) | `.venv/bin/ctbot-backtest` |
| Lab with next-close execution (stricter) | `.venv/bin/ctbot-lab --lag 1 --cash-apy 0.035` |
| Stock ranking model: walk-forward vs SPY, equal weight, momentum (dev + validation) | `.venv/bin/ctbot-rank-research` |
| Score the locked holdout once, after choosing | `.venv/bin/ctbot-rank-research --holdout` |
| Write the shadow model file for the bot (retrain monthly) | `.venv/bin/ctbot-rank-research --write-model` |
| Tests | `.venv/bin/pytest -q` |
| Known-vulnerability scan of installed packages | `.venv/bin/pip-audit --skip-editable` |

## Start at login (optional, macOS)

`scripts/autostart.sh install` loads the launchd agent `com.ctbot.bot`
(`scripts/com.ctbot.bot.plist.example`, installed to `~/Library/LaunchAgents/`). It runs
`./run_ctbot.sh` at login and restarts it if it stops (at most every 30 s), with
`CTBOT_NO_BROWSER=1` and `LIVE_TRADING_ENABLED=false`; log `logs/bot.log`.
`scripts/autostart.sh status` shows its state, `scripts/autostart.sh uninstall` removes it.
Run `install` again after moving the project folder.

## Scheduled job (optional)

On macOS, a launchd job can run the price-cache refresh, Collective2 tracking, idea scoring,
the 3/6/12-month performance figures and the health check on weekdays at 07:00 and 15:00 local
time (`scripts/c2_track_weekly.sh`, log `logs/c2-track.log`). To set it up, copy the template,
replace `/path/to/ctbot` with your project folder, and load it:

```bash
sed "s#/path/to/ctbot#$PWD#g" scripts/com.ctbot.c2-track.plist.example > ~/Library/LaunchAgents/com.ctbot.c2-track.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ctbot.c2-track.plist
```

Then:

```bash
launchctl kickstart gui/$(id -u)/com.ctbot.c2-track                                   # run it now
launchctl bootout gui/$(id -u)/com.ctbot.c2-track                                     # turn it off
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ctbot.c2-track.plist      # turn it back on
```
