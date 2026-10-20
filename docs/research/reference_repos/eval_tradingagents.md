# TradingAgents evaluation for CTBOT
Repo: TauricResearch/TradingAgents @ e8f5503 (v0.7.0), cloned at /home/claude/refs/TradingAgents (I left it unmodified, and I did not touch /home/claude/ctbot).
**Verdict:** don't adopt it as a component. It is a daily-bar, swing-horizon research pipeline built on LangGraph, with 15+ LLM calls and minutes of latency per decision.
Borrow three ideas (or small amounts of Apache-2.0 code): a bounded bull/bear debate, schema conventions, and outcome-settled reflection memory.

## 1. Install (headless)
```
/root/.local/bin/uv venv -p /usr/bin/python3.12 /home/claude/refs/envs/tradingagents
VIRTUAL_ENV=/home/claude/refs/envs/tradingagents uv pip install -e /home/claude/refs/TradingAgents pytest pytest-subtests
```
- Install worked. Upstream suite (`.../bin/python -m pytest -q`): 1361 passed, 1 skipped (bedrock extra), 24s, offline.

## 2. Smoke test (no keys, no network, no paid calls)
- Script: <scratch>/ta_smoke/smoke.py. It is adapted from the upstream harness tests/test_graph_end_to_end.py.
- Run: `cd <scratch>/ta_smoke && /home/claude/refs/envs/tradingagents/bin/python smoke.py`
- **What I mocked:**
  - `trading_graph.create_tier_client` returns a fake `BaseChatModel`. It calls every bound tool once, then returns canned text. Its `with_structured_output` returns canned pydantic objects.
  - Every entry in `dataflows.router.VENDOR_METHODS` is replaced with a stub string.
  - `snapshot.load_ohlcv` returns a synthetic 60-day DataFrame.
  - StockTwits and Reddit fetchers are stubbed, and `yf.Ticker` is stubbed for identity lookup.
- **What ran for real:** `TradingAgentsGraph(config)` and the LangGraph compile; `propagate("NVDA", "2026-01-09", portfolio=PortfolioContext(...))` through all 13 nodes (4 analyst subgraphs with ToolNode, Memory Log, Bull, Bear, Research Manager, Trader, 3 risk debaters, Portfolio Manager); rating parsing, memory-log write, state JSON dump.
- **Output:** `propagate ok in 0.12s; signal='Overweight'`.
  - 15 LLM calls in total: 3 tool-call turns, 8 text, and 4 structured (SentimentReport, ResearchPlan, TraderProposal, PortfolioDecision).
  - 12 vendor methods hit: stock_data, indicators, news, global_news, macro, prediction_markets, fundamentals, balance_sheet, cashflow, income_stmt, insider_transactions, ohlcv.
  - Prompt text alone was about 46k chars (roughly 11k tokens) with tiny mock data and 1-word reports.
- **Return value:** `(final_state, signal)`.
  - `signal` is one of Buy/Overweight/Hold/Underweight/Sell, or "REVIEW" when the rating can't be parsed. REVIEW is not a Hold (agents/rating.py).
  - `final_state` keys: market/sentiment/news/fundamentals_report, investment_debate_state, investment_plan, trader_investment_plan, risk_debate_state, final_trade_decision (markdown: Rating / Executive Summary / Investment Thesis / Price Target / Time Horizon), final_rating, past_context, portfolio_context, memory_note.
  - Files written: results/NVDA/TradingAgentsStrategy_logs/full_states_log_<date>.json, plus the memory log line `[2026-01-09 | NVDA | Overweight | pending]` followed by the decision.

## 3. Review
**License:** Apache-2.0 (LICENSE). It is compatible with CTBOT's MIT licence. Copied code needs the NOTICE/attribution kept and changed files marked.

**Roles and graph** (graph/setup.py, graph/conditional_logic.py):
- Analysts run in parallel, all on the quick tier: agents/analysts/{market,sentiment,news,fundamentals}_analyst.py. Each is a tool loop capped by `max_tool_rounds` (default 20).
- Research debate: agents/researchers/{bull,bear}_researcher.py, alternating for `max_debate_rounds` (default 1, so 1 turn each). It is judged by agents/managers/research_manager.py on the deep tier, which returns `ResearchPlan`.
- Trader: agents/trader/trader.py returns `TraderProposal` (Buy/Hold/Sell, entry, stop, sizing).
- Risk debate: agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py, round-robin for `max_risk_discuss_rounds`.
- Portfolio Manager: agents/managers/portfolio_manager.py on the deep tier returns `PortfolioDecision`. This is the only node that reads the memory lessons and the portfolio context.

**Structured decision** (agents/schemas.py):
- `PortfolioDecision{rating: 5-tier enum, executive_summary, investment_thesis, price_target?, time_horizon?}`.
- `TraderProposal{action, reasoning, entry_price?, stop_loss?, position_sizing: str?}`.
- Useful detail: `_coerce_optional_float` drops "N/A", "15%", ranges and hedges to None instead of failing the whole object.
- Structured output only where the provider supports it (else free text + regex parser). No confidence field, no numeric sizing.

**Memory and reflection** (memory/log.py, memory/settlement.py, memory/reflection.py):
1. After each run, `store_decision` appends a markdown entry tagged `pending`. No LLM call.
2. At the start of a later run, the "Memory Log" node runs `settle_all_pending`. For each pending entry whose `holding_period_days` window (default 5 trading days, entry at the first close after trade_date) has fully traded, `fetch_returns` computes the raw return and alpha against SPY (or a regional index) from yfinance daily closes.
3. Next, a single quick-tier LLM call (`Reflector`) writes a 2-4 sentence lesson.
4. `update_with_outcome` stores the return, alpha, holding window, reflection and `resolved:<date>`.
5. `get_past_context` injects the last 5 same-ticker and 3 cross-ticker lessons into the PM prompt. Backtests apply an `as_of` filter so lessons resolved after the trade date are excluded (point-in-time).

Outcomes are hypothetical (decision vs price move), not filled trades. Settlement is idempotent, file-locked, and keeps entries pending until the full window has traded.

**Portfolio context** (portfolio.py): a 64-line pydantic model `PortfolioContext{cash, currency, positions[ticker, qty, avg_price]}`. `render()` produces the prompt block, and `fingerprint()` keys the checkpoint. It keeps "no context" distinct from "flat book". Broker-neutral, read-only.

**Backtest** (backtest.py):
- Runs a grid of tickers x dates through propagate, using a separate memory log. It explicitly evaluates decisions only; there are no fills or P&L ledger. `summarize` reports per-rating hit rate and mean alpha.
- Look-ahead defenses (dataflows/date_window.py):
  - `as_of` / `as_of_window` clamp tool dates to trade_date.
  - `in_window` filters news, StockTwits and Reddit by publication time in UTC; undated items are dropped for historical runs.
  - `withhold_live_profile` hides `Ticker.info` from historical runs.
  - FRED uses realtime vintages, and SEC EDGAR statements are cut at filing date.
  - Tests cover these: tests/test_news_lookahead.py, test_fundamentals_lookahead.py, test_memory_pointintime.py, test_social_lookahead.py.
- **Historical news is not truly point-in-time on default vendors.** yfinance news only serves recent items, so for past windows `coverage_gap` returns an "unavailable" placeholder (no leak, but no news either).
  - Alpha Vantage NEWS_SENTIMENT supports `time_from`/`time_to` (key required, free tier rate-limited) and is the only vendor with real point-in-time news.
  - Reddit and StockTwits are recent-only.
- Look-ahead that remains and can't be fixed: the LLM's own training data knows how the market moved before its cutoff. The backtest's own summary says results are "indicative rather than repeatable".

**LLM calls per decision** (defaults: 4 analysts, debate=1, risk=1):
- Minimum 15, as measured (7 analyst + 2 research + RM + trader + 3 risk + PM). Realistic 20-40: analysts take several tool rounds, resending growing history.
- +1 reflection per newly settled past decision; +2 per extra debate round, +3 per extra risk round. Deep tier = RM and PM only.

**Token and cost estimate** (rough, from the call structure, not measured):
- The 4 reports (about 1.5-3k tokens each) are re-fed to about 9 downstream calls, plus debate histories and analyst tool payloads.
- Total is about 120k-300k input and 15k-40k output tokens per ticker-decision.
- At list prices: Sonnet 5.5 ($2/$10 per MTok) $0.40-1.00; Opus 5.5 ($4/$20) $0.80-2.00; Haiku 5.5 quick + Sonnet deep about $0.10-0.25.
- A 20-ticker daily scan therefore costs about $8-40/day with Sonnet. That is 15-40x CTBOT's single structured call.

**Latency:** about 9 sequential LLM hops after the parallel analysts, plus analyst tool loops. Expect about 2-10 minutes per ticker with reasoning models (estimated; I couldn't measure it without paid calls). The offline graph overhead is about 0.1s.

**Data vendors** (default_config.py `data_vendors`):
- yfinance (OHLCV, stockstats indicators, news, profile); SEC EDGAR then yfinance (statements); FRED (macro, FRED_API_KEY); Polymarket (keyless); optional Alpha Vantage (key); StockTwits/Reddit public endpoints (sentiment).

**Intraday suitability: poor.**
- `trade_date` is `YYYY-MM-DD`; OHLCV is daily, midnight-normalized (yahoo/ohlcv.py); outcomes scored over 5 trading days from the next close; prompts speak of "3-6 months" horizons; multi-minute latency rules out ORB/VWAP entry timing.

At most it could act as a pre-market, daily ETF/swing overlay (e.g. CTBOT's rsi2_etf or turn_of_month), not a per-signal intraday gate.

## 4. What CTBOT should borrow
CTBOT today (src/ctbot/decision/engine.py) makes one structured Claude call returning `DecisionSchema{approve, confidence, rationale, suggested_notional, stop/take_profit_pct}` and fails closed on any error. Keep that as the final gate.

**A. Bounded bull/bear step** (ideas only, optional per strategy; the multi-day ETF models first):
- Make 2 short calls ("strongest case FOR approving" and "strongest case AGAINST") on the same evidence, with low effort and max_tokens about 1k.
- Pass both into the existing judge call as context.
- Hard cap: 1 round. Run the two sides in parallel to keep latency at about 1 extra hop.
- Any failure skips the debate, not the gate (or rejects, if strict).
- Cost: +2 calls, about +2x input tokens, which is pennies per candidate on Sonnet/Haiku.
- Skip it for intraday ORB/VWAP, where latency matters.
- TradingAgents doesn't prove the debate adds alpha; measure it with a paper A/B before keeping it.

**B. Schema conventions** (small, worth copying with attribution):
- A nullish/percent coercer like `_coerce_optional_float`, so one bad optional field becomes None instead of rejecting.
- An explicit non-tradeable sentinel (TradingAgents' `REVIEW`) distinct from "reject", for audit stats.
- A `time_horizon`/`holding_period` field that must match the strategy's exit plan.
- Keep CTBOT's `extra="forbid"` plus local validation, which is stricter than TradingAgents.

**C. Outcome memory keyed by closed paper trades:** a better version of memory/settlement.py.
- Settle on actual closed CTBOT paper fills (entry/exit, P&L, alpha vs SPY over the real holding period), not a fixed 5-day hypothetical.
- On close, store `{strategy, symbol, decision_id, rationale, outcome, resolved_at}`. Optionally make one cheap Haiku reflection call, so about 1 call per closed trade.
- Inject the last N same-strategy and same-symbol lessons into the judge prompt, with an `as_of` filter so CTBOT backtests and replays never see future lessons.
- Use a SQLite table, not markdown. Keep the lessons count-capped; TradingAgents' `memory_log_max_entries` is the precedent.

**D. Portfolio context block:** CTBOT already passes `account`. Copy only the "no context ≠ flat book" rule and the fingerprint idea.

**Don't take:** LangGraph or the langchain-* dependency stack (heavy and churny), the vendor router, multi-provider clients, or the CLI.
None of TradingAgents touches brokers, so it carries no order risk; CTBOT's paper/live isolation is unaffected by any of the above.
