# Final report: reference-repo integration (2026-10-20)

Branch `claude/project-thread-cjsnts`, PR #7. Assessment and ranked plan:
`docs/research/2026-10-20-reference-repos.md`. How to run, costs, rollback:
`docs/RESEARCH_AND_OPERATIONS.md`.

## Status by component

| Component | Implemented | Tested | Mocked / synthetic | Blocked / deferred |
|---|---|---|---|---|
| Reference repos cloned, installed, smoke-tested (Qlib, TradingAgents, Vibe-Trading, OpenAlice) | yes, outside the repo, one env each | upstream suites: TradingAgents 1361 pass, Vibe-Trading 586, OpenAlice 1006; Qlib end-to-end pipeline | all data and LLMs mocked; no keys | — |
| Factor library (Alpha158 subset, pandas) | `research/factors.py` | point-in-time truncation test | synthetic prices | golden test against Qlib's `D.features` deferred |
| Walk-forward ranker + TopkDropout + baselines + ablations | `research/ranker.py`, `ctbot-rank-research` | purge, label timing, future-invariance, next-open fills, planted signal found / noise not found, end-to-end run | synthetic panels only | **real-data run blocked** (see below) |
| Trial registry, locked holdout, PBO, FDR, DSR trial count | `research/registry.py`, `research/validation.py` | chain tamper detection, holdout lock, PBO on noise vs skill, BH | synthetic | — |
| Lab next-close execution and PBO | `ctbot-lab --lag 1` | lag test | synthetic | real-data rerun blocked |
| Shadow ranker in the bot | `strategies/ranker_signal.py`, `[ranker]` shadow; TopkDropout on its own persisted book | live scoring equals research scoring; own book, not account positions; once per day; window; missing model | fake market data | forward paper record needs weeks of live running |
| Review spend cap, usage log, unreviewed shadow ideas | `llm_costs.py`, pipeline, settings | cap and default tests, cost arithmetic | fake Claude client | real spend measured only once the bot runs |
| Outcome memory for reviews | `decision/memory.py` | as-of filtering | fake ledger | value of the memory unmeasured |
| Bull/bear debate (off) | `ClaudeDecisionEngine.debate` | adds 2 low-effort calls; failure never blocks | fake client | no evidence it helps; stays off |
| Stale quote, HALT file, duplicate window | risk manager, pipeline | dedicated tests, incl. CSV mode, trims under HALT, retry after no fill | — | — |
| Decision journal and explicit no-trade | `contracts.py`, pipeline | record order and outcomes | — | — |
| Premarket plan, EOD reconciliation | `dayplan.py`, scheduled job | holiday/HALT reasons; mismatch detection | synthetic ledger | — |
| Thesis files with code-checked invalidation (OpenAlice idea) | partly: model stops, `exit_at`, exit signals already do this | — | — | separate thesis files deferred: low value over existing brackets |
| News/event-driven, pairs, intraday variants | rejected with reasons in the assessment | — | — | — |

Test suite: 204 passed (`.venv/bin/pytest -q`; 177 before). The repository has no CI workflow,
so "green" means the local suite.

## Independent review

A separate review of the branch found four bugs, all fixed with regression tests: the stale-quote
check refused every entry in CSV mode; the live ranker ran TopkDropout on the account's positions
instead of its own picks (and `topk_dropout` could buy while above k); HALT blocked the core's
risk-reducing trims; and the duplicate guard blocked a retry of an exit that didn't fill.

## Real-data results (run 2026-10-20 on the operator's Mac)

Dataset: Robinhood split-adjusted daily bars, no dividends, 1999-09-15 to 2026-10-16
(`data/bars_cache`, fingerprint 33ea388d1a903dfc). Holdout (2023 onward) **not scored**.

**Ranker** (`ctbot-rank-research`): SPY plus 60 of today's large caps (survivorship-biased), 16
trials, dev to 2017-12-31 and validation 2018-2022, next-open fills, 4 bps per side.

- No ranker variant beats equal weight after Benjamini-Hochberg (q=10%).
- PBO 0.62: picking the best setting is more likely luck than skill.
- Validation IC is about 0.00 for h=5 and 0.013 for h=21.
- On dev, 12-1 momentum (15.9% CAGR, Sharpe 0.82) beat every ranker variant; the best was
  k=3 h=5 at 11.7%, 0.70.
- Validation CAGRs up to 22% came with -50% drawdowns, against -47.5% for momentum.

Verdict: negative. The ranker stays in shadow and no model file is written; the holdout stays
unspent.

**Lab with next-close fills** (`ctbot-lab --lag 1`, 3.5% cash yield): out of sample 2010-2026,
1999-2009 in sample; catalog PBO 0.16.

| Strategy | OOS CAGR | Max DD | Sharpe | DSR |
|---|---|---|---|---|
| Vol-target QQQ 15% + trend | 15.9% | -13.2% | 1.14 | 0.96 |
| Current allocation: 40% QQQ trend + 40% QLD capped + 20% cash | 20.0% | -21.0% | 1.11 | 0.97 |
| Trend QQQ 170d, then QLD capped at 30% vol | 28.9% | -26.5% | 1.06 | 0.96 |
| Current core: trend QQQ 170d | 19.0% | -24.2% | 1.06 | 0.96 |
| Hold QQQ | 19.3% | -35.6% | 0.84 | 0.95 |

The trend cores hold up when they trade one close after the signal, so the current allocation
stands. 3x ETFs without a volatility cap lost 100% over the full sample. The lab printed a complex
CAGR for those wiped-out rows; it is now clamped to -100%.

There is no backtest of the Claude-reviewed short-term setups: an LLM can't be backtested without
look-ahead, so they are measured only forward, in paper.

## Synthetic checks

The machinery was first checked on synthetic prices:

- Planted-signal panel (40 stocks, 2012-2023, synthetic): the ranker found the signal
  (validation IC 0.13) and beat equal weight after FDR. Expected by construction; proves the
  pipeline can detect an edge, not that one exists.
- Pure-noise panel (60 stocks, ~16 years, synthetic, 16 trials): no ranker variant beat equal
  weight after FDR (q=10%). Proves it doesn't invent one.

## Reproducing

Run on the machine that holds the price cache (the build environment cannot reach market data):

```bash
.venv/bin/ctbot-fetch-bars --interval day --since 2010-01-01 \
  $(.venv/bin/python -c 'from ctbot.research.ranker import default_universe as u; print(" ".join(u()))')
.venv/bin/ctbot-rank-research
.venv/bin/ctbot-lab --lag 1 --cash-apy 0.035
```

## What changes for the running bot after merge

- Shadow-model ideas are no longer sent to Claude (`REVIEW_SHADOW_IDEAS=false`); they appear as
  "Watching only · not reviewed". Trading models are reviewed as before, up to $1.00/day.
- Reviews also see each model's settled paper record (no extra call).
- A new `ranker` model appears on Discover (Watching); it does nothing until a model is trained.
- `touch data/HALT` stops new entries. Quotes older than 120 s block entries.
- `data/journal.jsonl`, `data/llm_usage.jsonl`, `data/day_plan/`, `data/eod/` start filling.

Nothing changes live trading (`LIVE_TRADING_ENABLED=false`, `live/` untouched) or account limits.

## Recommended next steps (in order)

1. Leave the ranker in shadow; don't spend the holdout on it. Revisit with a broader,
   point-in-time universe if one becomes available.
2. Keep the trend-core allocation, which held up with next-close fills.
3. After a week of measured spend, compare Haiku vs Opus reviews with `ctbot-score-ideas`; at $250
   capital, Haiku is the default worth testing.
