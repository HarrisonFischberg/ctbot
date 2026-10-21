# Research loop, day workflow, costs and rollback

What the 2026-10-20 integration added, how to run it, what it costs, and how to undo it. The
assessment of the four reference projects is in `docs/research/2026-10-20-reference-repos.md`.

## Layers

| Layer | Module(s) | LLM? |
|---|---|---|
| Research and idea generation | `research/ranker.py`, `research/lab.py`, `research/backtest.py`, `research/registry.py`, `research/validation.py` | No |
| Deterministic features | `research/factors.py` (Alpha158 subset, point-in-time) | No |
| Strategy signals, model predictions | `strategies/*` incl. `strategies/ranker_signal.py` (frozen model file) | No |
| Review | `decision/engine.py` (one fail-closed call; optional debate), `decision/memory.py` (outcome memory, code-computed) | Yes: proposes and explains only |
| Allocation and risk limits | `risk/manager.py` (sizes, caps, PDT, stale quotes, kill switch), `allocations.py` | No |
| Execution and reconciliation | `pipeline._submit` (HALT file, duplicate window), `paper/simulator.py`, `dayplan.reconcile` | No |
| Evaluation, monitoring, feedback | `contracts.py` journal, `llm_costs.py`, `research/score_ideas.py`, `dayplan.plan`, dashboard | No |

Every order still goes through `RiskManager.check_entry` / `check_exit` and then `_submit`.
Quantities, prices, exposure and P&L are computed in code; Claude's output is a yes/no,
a confidence and a rationale, and its suggested size can only shrink a position.

## Governed research loop

1. **Propose**: a strategy or parameter set, written down before results are seen (the trial list
   in `ranker.trials()` is fixed in code).
2. **Implement** as a catalog entry (a parameter set on an existing type), not generated code.
3. **Backtest**: `ctbot-rank-research` (daily stock ranking) or `ctbot-lab --lag 1` (portfolio
   strategies, trading one close after the signal). Every trial is appended to
   `data/research/trials.jsonl` (hash-chained; `Registry().verify()`), kept or not.
4. **Validate out of sample**: choose only on the `dev` (to 2017) and `validation` (2018-2022)
   splits. Check IC, turnover, drawdown, regime split, concentration of returns, PBO (~0.5 means
   the pick is luck), the Deflated Sharpe with the registry's trial count, and whether the excess
   over equal weight survives Benjamini-Hochberg at q=10%.
5. **Holdout once**: `ctbot-rank-research --holdout` scores 2023-now. A second holdout run of the
   same trial is refused unless forced, and a forced run is marked `holdout_rerun` in the registry.
6. **Paper (shadow)**: `ctbot-rank-research --write-model` writes `data/research/ranker_model.json`;
   the `ranker` model (shadow in `config/strategies.toml`) then records ideas every Monday
   09:35-10:00 ET with no Claude cost.
7. **Compare against the baseline**: after enough forward weeks, compare the ranker's shadow ideas
   (`ctbot-score-ideas`, the journal) with the trend core and SPY over the same dates.
8. **Promote**: only you can switch a model to trade (dashboard or `strategies.toml`). No model,
   model file, LLM or assistant proposal changes its own mode; the risk limits apply either way.

### Reproducing the ranker experiment

```bash
.venv/bin/ctbot-fetch-bars --interval day --since 2010-01-01 \
  $(.venv/bin/python -c 'from ctbot.research.ranker import default_universe as u; print(" ".join(u()))')
.venv/bin/ctbot-rank-research                     # dev + validation only; writes data/research/ranker_report.json
.venv/bin/ctbot-rank-research --extra-cost-bps 5  # cost sensitivity
.venv/bin/ctbot-rank-research --holdout           # once, after choosing
.venv/bin/ctbot-rank-research --write-model       # shadow model file for the live bot
.venv/bin/ctbot-lab --lag 1 --cash-apy 0.035      # lab with next-close execution (leaderboard_lag1.json)
```

Fixed assumptions: universe = SPY + the 60 large caps in `research/lab.py` (survivorship-biased:
today's winners); label = open(t+1) → open(t+1+h); ridge on cross-sectionally ranked factors,
rolling 756-day window, refit every 21 days, purged by h+1 days; TopkDropout, equal weight,
fills at the next open; costs 4 bps per side for stocks, 2 bps for ETFs, plus `--extra-cost-bps`;
liquidity filter price ≥ $5 and 20-day dollar volume ≥ $20M; split-adjusted prices without
dividends (Robinhood daily bars), so every strategy and benchmark understates total return.

Trials (16 per run): SPY, equal weight, 12-1 momentum (±regime), ranker k∈{3,5} × h∈{5,21} ×
{no regime, SPY>200d regime}, and four ablations of k=3 h=5 without each feature group.

## Day workflow

| When (ET) | What | Command / module |
|---|---|---|
| ~09:00 (scheduled job) | Plan: session or holiday, early close, which models trade vs watch, risk headroom, HALT, Claude budget, data/model freshness; verdict "entries possible" or "no trade" with reasons | `ctbot-dayplan` → `data/day_plan/<date>.json` |
| 09:30-09:35 | Observe; the opening range forms. No entries from the ranker before 09:35 | — |
| 09:35-11:00 | Setup windows: ORB / scanner ORB, ranker (Mondays, on yesterday's close) | `ctbot-paper --loop --interval 300` |
| every 5 min | Scan cadence = the 5-minute bar the intraday models use; quotes older than 120 s are refused; brackets, time exits and thesis invalidation (`stop`, `exit_at`, the model's own exit signals) run first | `pipeline.manage_positions` |
| 15:30-15:44 | Multi-day models decide on the live price | trend core, RSI(2), turn of month |
| 15:45-16:00 | No new entries in the last 15 minutes | `NO_ENTRIES_LAST_MINUTES` |
| after the close (scheduled job) | Reconcile ledger vs fills vs brackets vs journal; realized P&L per model; cycles and no-trade cycles; Claude spend | `ctbot-reconcile` → `data/eod/<date>.json` |

Each cycle writes a `cycle` record to `data/journal.jsonl` with outcome `traded` or `no_trade`
and the reasons (no candidates, rejected by risk, shadow only, review budget, HALT).

## Safety controls added

| Control | Where | Behaviour |
|---|---|---|
| Stale quote | `RiskManager.check_entry` | entries refused when the quote is older than `MAX_QUOTE_AGE_S` (120 s); the Robinhood source already drops stale quotes, this is the second check. Not applied in CSV mode, whose quotes are the last daily close |
| Kill-switch file | `pipeline.halted` | `touch data/HALT`: no new entries or top-ups (automatic or dashboard); exits and risk-reducing trims still run |
| Duplicate orders | `pipeline._submit` | a second buy of the same symbol within 120 s of a filled buy is refused; sells and retries of unfilled orders are never blocked |
| Spend cap | `pipeline` + `llm_costs` | no paid review once today's measured spend reaches `MAX_CLAUDE_USD_PER_DAY` |
| Reconciliation | `ctbot-reconcile` | non-zero exit on any ledger/bracket/journal mismatch |

Existing controls (unchanged): paper/live process isolation, `LIVE_TRADING_ENABLED=false`,
position and capital caps, daily loss halt, drawdown kill switch (latched), PDT budget, spread and
liquidity limits, earnings guard, per-model losing-streak pause, single-instance lock, persisted
risk state for restarts.

## Operating costs

| Item | Cost | Notes |
|---|---|---|
| Claude reviews, Opus 5.5 ($4/$20 per M tokens) | ~$0.05 per review (est. 4k in / 1.5k out incl. thinking) | capped at $1.00/day by default (~$250/yr worst case vs $250 capital) |
| Claude reviews, Haiku 5.5 ($0.10/$0.50) | ~$0.001 per review | `CLAUDE_MODEL=claude-haiku-5-5`; check quality with `ctbot-score-ideas` first |
| Debate (`REVIEW_DEBATE=true`) | +2 short calls per review (~+60%) | off by default; no evidence yet that it helps |
| Shadow ideas | $0 by default | were paid reviews before this change |
| Ranker, factors, research tools | $0 | local CPU; a full `ctbot-rank-research` run takes about a minute |
| Market data | $0 | Robinhood read-only with your account; TradingFlow / Marketaux / Webull optional keys |
| Infrastructure | $0 | your Mac; no new services or subscriptions |

Measured spend: `python -c "from ctbot.llm_costs import summary; from pathlib import Path; print(summary(Path('data/llm_usage.jsonl')))"`.
The review estimate above is not measured yet; the log replaces it after a few trading days.

## Limitations to keep in mind

- The ranker's universe is today's large caps: survivorship bias inflates absolute returns. Compare
  it with equal weight and momentum on the same list rather than reading its CAGR.
- Prices exclude dividends; the lab assumes same-close execution unless run with `--lag 1`.
- No historical LLM backtest is attempted: a model can know what happened after its training
  data's dates, so the review's value is measured only forward (`ctbot-score-ideas`).
- News/event-driven setups are not tested: there is no point-in-time news archive.

## Rollback

| Undo | How |
|---|---|
| Everything | revert the merge commit of this change; no data migration is involved |
| Ranker model only | `enabled = false` under `[ranker]` in `config/strategies.toml`, or delete `data/research/ranker_model.json`; delete `data/research/ranker_book.json` to reset its picks |
| Old review behaviour | `REVIEW_SHADOW_IDEAS=true`, `REVIEW_OUTCOME_MEMORY=false`, `MAX_CLAUDE_USD_PER_DAY=1000` |
| Stop new entries now | `touch data/HALT` (delete to resume) |
| New files are additive | `data/journal.jsonl`, `data/llm_usage.jsonl`, `data/research/`, `data/day_plan/`, `data/eod/` can be deleted at any time |
