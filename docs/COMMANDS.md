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
| The same in the background, restarted if it stops (optional, macOS) | see [Run in the background](#run-in-the-background-optional) |
| Paper bot only (macOS: kept awake from 45 minutes before the open to the close) | `.venv/bin/ctbot-paper --loop --interval 300` |
| Dashboard only | `.venv/bin/ctbot-dashboard` then open http://127.0.0.1:8787 |
| Pre-open health check | `.venv/bin/ctbot-preflight` |
| Robinhood sign-in (when preflight says it expired) | `.venv/bin/ctbot-robinhood-login` |

Only one paper bot can run at a time (a lock refuses a second). Start it before 9:30 ET;
the scanner model needs it running from 9:36 ET, the trend core trades 15:30-15:44 ET.

You can leave it running. Outside the session (nights, weekends, NYSE holidays; 13:00 ET on
half days) it does no analysis and fetches no quotes: it records the closing equity, lets the
Mac sleep, and resumes at the next open. The dashboard stops polling too: while the market is
closed it shows the last saved prices with their time ("prices as of ..."), fetching once after
the close only if it wasn't running near the bell. From 45 minutes before the open the bot holds
the Mac awake again (`caffeinate -dis`). A sleeping Mac doesn't wake itself, so schedule a weekday
wake inside that window, between 08:45 and 09:25 ET, e.g. 06:55 Mountain (covers the 07:00
refresh job too):

```bash
sudo pmset repeat wakeorpoweron MTWRF 06:55:00
```

Keep the Mac plugged in, with the lid open (or closed with an external display attached). With
the lid closed and no display, the scheduled wake is only a dark wake (display off), and Apple
says `-d` and `-i` have no effect then. `-s` is the one assertion that can keep it running, and
macOS honors it on AC power only. Closed-lid running on AC hasn't been tested yet. On
battery with the lid closed the Mac goes back to sleep and the bot misses the open.

## Run in the background (optional)

`./run_ctbot.sh` runs only while its Terminal window is open, and nothing restarts the bot if it
stops. On macOS, two per-user LaunchAgents can run it instead:

- `com.ctbot.paper` (`scripts/com.ctbot.paper.plist.example`) runs
  `ctbot-paper --loop --interval 300`. It starts when you log in and restarts after it stops for
  any reason, at most once a minute. Its log is `logs/ctbot-paper.log`, as before, plus any
  startup errors and tracebacks.
- `com.ctbot.dashboard` (`scripts/com.ctbot.dashboard.plist.example`) runs the dashboard, with
  the same rules, logging to `logs/dashboard.log`. It's optional: the bot trades without it, so
  skip it if you'd rather start `.venv/bin/ctbot-dashboard` only when you want it.

Use the agents or `./run_ctbot.sh`, never both: only one paper bot can hold the ledger lock.
While `com.ctbot.paper` is loaded, `./run_ctbot.sh` says so and exits (with only the dashboard
agent loaded, it starts just the bot). If you load the agent while `./run_ctbot.sh` is still
running, the agent's bot is refused once a minute until that one stops, then takes over.

Stop `./run_ctbot.sh` first (Ctrl+C), then install both and start them:

```bash
mkdir -p ~/Library/LaunchAgents
for job in paper dashboard; do sed "s#/path/to/ctbot#$PWD#g" scripts/com.ctbot.$job.plist.example > ~/Library/LaunchAgents/com.ctbot.$job.plist; done
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ctbot.paper.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ctbot.dashboard.plist
```

`bootstrap` starts an agent right away; after that it starts at every login. Then:

```bash
launchctl print gui/$(id -u)/com.ctbot.paper | grep -E '^.(state|pid|last exit code) = '  # is it running?
tail -f logs/ctbot-paper.log                                                                # follow the bot
launchctl kickstart -k gui/$(id -u)/com.ctbot.paper                                        # restart it (after editing .env or config/)
launchctl bootout gui/$(id -u)/com.ctbot.paper                                             # stop it
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ctbot.paper.plist              # start it again
```

The same commands work for `com.ctbot.dashboard`. `kill` doesn't stop an agent, because launchd
starts it again within a minute. `bootout` stops it until your next login. To remove the agents
for good:

```bash
for job in paper dashboard; do launchctl bootout gui/$(id -u)/com.ctbot.$job; rm ~/Library/LaunchAgents/com.ctbot.$job.plist; done
```

Limits:

- The agents run only while you're logged in. After a restart they start when you log in. With
  FileVault on, macOS can't log in by itself, so a restart (an overnight update, say) leaves the
  bot stopped until you log in. Logging out stops them too.
- They don't wake a sleeping Mac. The `pmset` schedule above does that.
- If you move the project folder, install them again.

## Dashboard: Settings and the strategy launcher

- **Settings tab**: paste API keys (Anthropic, Collective2) and the Robinhood account number and
  nickname. They are saved in `~/.ctbot/settings.json` (only your user can read it), override
  `.env`, and are never shown again or committed. **Connect Robinhood** opens the read-only
  sign-in in your browser; **Run check** tests every connection. Restart the bot after a change (with the background
  agent: `launchctl kickstart -k gui/$(id -u)/com.ctbot.paper`).
- **Home**: portfolio value, a 4-step *Get started* checklist, and *Your strategies* (what you
  invested, value now, total return, Stop).
- **Discover**: every strategy in plain words with its simulated return over the past 3, 6 or 12
  months next to the S&P 500. **Invest** asks for an amount, a max loss and a take-profit; that
  strategy then trades on its own with that much paper money (never more), and the bot sells and
  stops it when the max loss or take-profit is reached. Every other model only suggests ideas
  while anything is invested. **Stop** sells what it bought at the next market check. With
  nothing invested, `config/strategies.toml` decides what trades. Paper only.
- **Activity**: the bot's trade ideas, positions, order history, safety limits and log.
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

## Research

| What | Command |
|---|---|
| Strategy lab: leaderboard, Deflated Sharpe, walk-forward, crash stress test | `.venv/bin/ctbot-lab --cash-apy 0.035` |
| Trend length sweep (chosen on 2010-19, checked on 2020-26) | `.venv/bin/ctbot-sweep` |
| Tearsheet for a lab strategy | `.venv/bin/ctbot-tearsheet --lab "lev: trend QQQ 170d -> QLD capped at 30% vol"` |
| Intraday backtest (5-minute data) | `.venv/bin/ctbot-backtest` |
| Tests | `.venv/bin/pytest -q` |
| Known-vulnerability scan of installed packages | `.venv/bin/pip-audit --skip-editable` |

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
