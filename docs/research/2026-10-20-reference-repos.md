# CTbot vs TradingAgents, OpenAlice, Vibe-Trading and Qlib (2026-10-20)

CTbot is this repository (`HarrisonFischberg/ctbot`, package `ctbot`). "cabot"/"CTBOT" in the
request mean this project. Detailed per-repo notes, install commands and smoke-test output are in
`docs/research/reference_repos/`.

## 1. CTbot as it stands (audited on `main` @ 9b82f94; 177 tests pass)

| Area | What exists and works | Gaps found |
|---|---|---|
| Account | Robinhood cash account, read-only market data; paper only. `$250` trading capital cap, max 3 positions, long only, fractional, PDT 3 day trades / 5 days | Live executor is a guarded stub (by design) |
| Data | Robinhood daily + 5-min bars (5-min history only since late Feb 2026), quotes rejected after 120 s; Webull/TradingFlow daily backups; TradingView/Cboe/Marketaux extras | No point-in-time news archive; risk manager trusts the provider to drop stale quotes |
| Strategies | 16 models in `config/strategies.toml`. Trading: `trend_core` (QQQ>170d, 40%), `trend_core_qld` (QLD, vol-capped, 40%), `orb_sip` scanner ORB (20% sleeve). Shadow: momentum, mean reversion, alt-data clusters, options flow, ORB, VWAP, overnight, RSI(2), turn-of-month, trend template | Intraday models showed no edge after costs (`docs/strategy_research_2026-10-14.md`); no cross-sectional/predictive model |
| Decision | Ensemble → Claude review (one call, JSON schema, fails closed) → deterministic `RiskManager` → paper broker. Systematic models skip Claude | Shadow-only candidates still get a paid review; no outcome feedback into reviews; no LLM cost accounting |
| Risk | Size caps, daily loss halt, drawdown kill switch, spread/liquidity, PDT budget, earnings guard, per-model losing-streak pause, single-instance lock, persisted risk state | No independent quote-age check, no file kill switch, no duplicate-order window |
| Research | `ctbot-backtest` (5-min, next-bar fills, stop-first), `ctbot-lab` (daily portfolios, Deflated Sharpe, yearly walk-forward, crash stress), `ctbot-sweep`, `ctbot-score-ideas`, `ctbot-perf` | Lab fills at the same close the signal uses; no trial registry; no PBO; no locked final holdout; universe of today's large caps (survivorship bias) |
| Ops | Dashboard (loopback), Bub assistant (proposals only), launchd autostart, preflight, scheduled refresh job | No premarket/EOD plan or reconciliation record |

**LLM cost is material at this account size.** Reviews run on `claude-opus-5-5` ($4/$20 per M
tokens). At ~4k input + ~1.5k output (incl. thinking) a review costs roughly $0.05; the default
cap of 40/day allows ~$2/day, i.e. up to ~$500/yr against $250 of capital. Even 5 reviews/day is
~25% of capital per year, far above any edge measured so far. (Estimate from prices and prompt
size, not measured usage; the new cost log measures it.)

## 2. Reference repositories (commit, license, verdict)

| Repo @ commit | License | Smoke test (all offline, mocked data/LLM) | Intraday fit | Verdict |
|---|---|---|---|---|
| Qlib @ 5435523 | MIT | `pyqlib` 0.9.7 wheel; Alpha158 handler (158 features), LightGBM + ridge, TopkDropout backtest, `risk_analysis` on synthetic data: all ran | Batch only; official data feed disabled | Port factors, walk-forward and TopkDropout as pure pandas. Not a dependency (1.1 GB, mlflow drift, 238-day CN annualisation default) |
| TradingAgents @ e8f5503 | Apache-2.0 | 1361 upstream tests pass; full `propagate()` with fake LLM: 15 LLM calls, 13 nodes | Poor: daily dates, 15–40 calls and minutes per decision | Borrow ideas: bounded bull/bear step, outcome memory keyed by settled trades, as-of filtering. Not LangGraph |
| Vibe-Trading @ 097b29f | MIT (+NOTICE) | 586 tests pass; engine backtest on synthetic data; same-bar peek strategy neutralised by its 1-bar shift | Generic bars, no PDT/intrabar stops | Borrow ideas: PBO/false-discovery control, lookahead regression test, run cards, hash-chained ledger, HALT file. Its Monte Carlo and "walk-forward" helpers are weaker than CTbot's |
| OpenAlice @ 87bc415 | **AGPL-3.0** | 1006 vitest tests pass with `MockBroker`; trading service started on loopback with no accounts | n/a (agent shell) | No code may be copied. Clean-room ideas only: staged/approved orders (CTbot already has manual approval), thesis files with code-checked invalidation, scheduled investigations |

## 3. Gap → source mapping, ranked

Score = expected value × evidence ÷ (complexity, cost, operational risk). All items are
deterministic code unless marked LLM.

| # | Gap in CTbot | Borrowed from | Value | Evidence today | Effort | Risk |
|---|---|---|---|---|---|---|
| 1 | Credible research loop: trial registry, locked holdout, PBO, next-bar fill option for the lab | Vibe-Trading `quantlib/multipletesting.py`, `run_card.py`; qlib `RollingGen` | High: protects every future decision | Strong (method) | S | Low |
| 2 | Cross-sectional predictive ranking with purged walk-forward and TopkDropout, compared with momentum, equal weight and SPY | qlib `contrib/data/loader.py` (Alpha158), `contrib/strategy/signal_strategy.py` (TopkDropout) | Medium–high if it survives OOS; unknown | None yet (needs data) | M | Low (shadow, systematic, no LLM) |
| 3 | LLM cost control and measurement: no paid review for shadow-only ideas by default, per-call usage log, daily $ budget | own; TradingAgents cost analysis | High at $250 capital | Strong (arithmetic) | S | Low |
| 4 | Risk defence in depth: quote age in `check_entry`, HALT file, duplicate-order window | Vibe-Trading `live/halt.py`, OpenAlice guards (ideas) | Medium | n/a | S | Low |
| 5 | Explicit contracts and a decision journal (snapshot, strategy result, proposed order, outcome; timestamps, sources, versions) | TradingAgents state log, Vibe-Trading run card | Medium (auditability) | n/a | S | Low |
| 6 | Outcome memory: per-model/per-symbol settled paper results fed (as-of) into the review | TradingAgents `memory/log.py`, `reflection.py` (no LLM reflection) | Medium | Weak | S | Low |
| 7 | Day plan + EOD reconciliation with an explicit "no trade" record | OpenAlice scheduler idea | Medium (ops) | n/a | S | Low |
| 8 | Thesis invalidation checks for open positions | OpenAlice `build-thesis` idea | Low–medium (brackets already cover most) | Weak | S | Low |
| 9 | Bull/bear debate before the Claude verdict (LLM, off by default) | TradingAgents `agents/researchers/*` | Unknown; +2 calls | None; contamination risk | S | Cost |

Rejected: running any of the four apps as a service; LLM-generated strategy code (Vibe-Trading)
since generated strategies must not self-deploy and parameters on catalog strategies are safer;
Qlib's intraday/RL stacks; TradingAgents' full graph for intraday decisions (latency, cost).

## 4. Strategy families against CTbot's constraints

| Family | Decision |
|---|---|
| Opening-range breakout | Already tested (no edge on 88 large caps; `orb_sip` scanner sleeve trades as a forward test). No new variant |
| Intraday momentum / trend | Tested and rejected 2026-10-14 (-3 to -8 bps OOS) |
| VWAP pullback / mean reversion | Tested and rejected (negative both periods) |
| Relative strength / sector rotation | Exists in the lab (`sector_rotation`, `dual_momentum`); the new ranker generalises it |
| Volatility / regime-conditioned | Exists (`trend_core_qld` vol cap; regime label). Ranker gets an SPY>200d regime ablation |
| News / event-driven | Rejected for now: no point-in-time news archive, so no honest backtest; Marketaux sentiment stays a review input only |
| Pairs / stat-arb | Rejected: cash account, no shorting, $250 capital |
| Qlib-style predictive ranking | **Implement** (shadow, systematic), gated by walk-forward + holdout + PBO |

## 5. Implementation order

1. Research governance (registry, holdout lock, PBO/BH, lab fill lag) and the factor library.
2. Walk-forward ranker + TopkDropout + ablations, `ctbot-rank-research`, plus a shadow live strategy that reads a frozen model file.
3. Review cost controls and usage log.
4. Risk defence in depth, contracts and decision journal.
5. Outcome memory, day plan / reconciliation, thesis checks, optional debate.

Data blocker: the price cache (`data/bars_cache/`) lives only on the operator's Mac and is not in
the repo; this build environment cannot reach market-data hosts. Every component is tested on
synthetic data with planted signals and planted lookahead traps here; real-data results come from
running `ctbot-rank-research` against the cache.
