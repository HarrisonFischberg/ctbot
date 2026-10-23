# Day-trader skills (installed project-local)

The eleven `trader-*` folders here are Jake Schincariol's
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) pack (MIT, see
`LICENSE-day-trader-skill`), copied unchanged at commit `3d0f4ef`. Claude Code picks up
project skills from `.claude/skills/`, so in this repo they run as `/trader-scanner`,
`/trader-news` and so on. The tools are standard-library Python on free public data and
write to a `trader/` folder in the repo root (git-ignored). They are separate from the bot:
nothing in `src/ctbot` imports them, and they never touch the bot's ledger.

To update: copy the folders again from a fresh clone of the pack.

## What each skill became inside CTbot

| skill | in the bot |
| --- | --- |
| trader-scanner | `ctbot.analysis.setups`: the five daily-close rules (20-day breakout, bull flag, 20-day-average reclaim, oversold, oversold bounce) in every Claude review (`chart_setups`) and in `ctbot-dayplan`'s premarket plan |
| trader-news | `ctbot.market_data.sec_edgar`: each company's 8-K item codes of the last two weeks in reviews (`sec_8k`); a bankruptcy (1.03), delisting notice (3.01) or non-reliance (4.02) 8-K blocks new entries (`sec_8k_red_flag`). Needs `SEC_USER_AGENT`. Headlines are not passed on: only codes and numbers reach Claude |
| trader-entry | `orb_sip` waits for a 5-minute bar to close above the range (`confirm = "close"`, the default; `"touch"` is the paper's rule), optionally with `min_volume_x` |
| trader-exit | risk checks on a model's own plan: a stop under 0.05% from the entry is refused (`stop_too_tight`, on), optional `MIN_REWARD_RISK`; every closed trade's result in R (`r_multiple` in the trade log) |
| trader-timemachine | `ctbot.research.timemachine` / `ctbot-timemachine`: 10,000 seeded replays of the rest of the session from the stock's own 5-minute bars, plus the same plan on each past session; reviews of intraday ideas get the odds (`replay_odds`) |
| trader-risk | optional `MAX_OPEN_RISK_PCT` (loss at every open stop plus the new one), `MAX_ENTRIES_PER_DAY`, `NO_ENTRIES_BEFORE` |
| trader-budget | optional `MAX_LOSS_PER_TRADE_PCT`: the stop decides the size of a reviewed buy |
| trader-alerts | Bubs Thoughts notes when a held stock nears its stop or is up 1R (once a day each) |
| trader-analyst | `sec_edgar` again: the last reported quarter (revenue, net income, EPS against a year earlier, margins) in reviews (`reported_numbers`) |
| trader-pelosi | `house_clerk` provider: free House PTRs as congress-trade events for the `congressional` strategy (`ALT_DATA_PROVIDERS=...,house_clerk`, needs `pdftotext`) |
| trader-broker | enabled as a skill for paper orders on a connected broker (see below); not wired into the bot's own executor |

## trader-broker in this repo

Enabled (Harold, 2026-10-23), so the order step can be tried before the bot ever places orders
itself. It stays on a **paper** account (Alpaca's paper account by default) and every order
needs all three of its gates: trader-risk passed that exact order today, you typed yes for that
one order in the chat, and you clicked Allow once on Claude's tool card (set the broker's order
tool to "Needs approval", never "Always allow"). Broker keys go in the Claude Desktop config,
never in the chat or this repo. It is a separate path from the bot: CTbot's own executor is
still the paper simulator, the live adapter stays disabled, and Robinhood order tools stay
forbidden. Wiring real orders into the bot is its own future change.
