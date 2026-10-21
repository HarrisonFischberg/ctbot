# Vibe-Trading (HKUDS) evaluation for CTBOT
Repo: /home/claude/refs/Vibe-Trading @ 097b29f (v0.1.16). Venv: /home/claude/refs/envs/vibetrading (py3.12, 752 MB).
No keys used, no network data fetched, no broker/LLM calls, no frontend/desktop installs. /home/claude/ctbot untouched.

## Verdict
Don't add it as a dependency. It is a big multi-market agent platform: about 196 pinned packages including langchain,
langgraph, ccxt, akshare, tushare, fastmcp, weasyprint and broker SDKs, plus about 20 broker connectors with live
order paths (Robinhood among them). It is far too much surface for a paper-only bot with a read-only Robinhood
allowlist. Borrow two small, self-contained MIT modules (multiple-testing and purged CV) and a few ideas.

## 1. Install (headless)
    uv venv -p /usr/bin/python3.12 /home/claude/refs/envs/vibetrading
    uv pip install -p .../bin/python -r requirements-lock.txt      # 9 s, hash-pinned, no errors
    uv pip install -p .../bin/python --no-deps -e . pytest
The full lock installed cleanly, so no reduced install was needed. Note: the lock pulls broker/LLM SDKs even for headless use.

## 2. Smoke test (offline)
Script: <scratch>/vt/smoke.py
(run with HOME set to a scratch dir so nothing lands in ~/.vibe-trading)
- Data: synthetic GBM OHLCV for AAPL.US/MSFT.US, 1500 business days. Mocked: FakeLoader.fetch() replaces the
  network loaders (yfinance etc.); the engine is called directly (GlobalEquityEngine(cfg, market="us").run_backtest)
  rather than through backtest/runner.py, which would route to network loaders.
- Strategy A, SMA20>SMA100 long-only: total +21.4%, Sharpe 0.26, MaxDD -42%, 19 trades, PF 1.26.
  MC p(Sharpe)=0.40, bootstrap Sharpe 95% CI [-0.56, 1.05], "walk-forward" consistency 0.4. All correctly not significant.
- Strategy B, a deliberate lookahead signal (close>open on the same bar): -36.5%, Sharpe -0.27, 663 trades.
  The engine's one-bar signal shift neutralised the peek, as intended.
- Fill check: MSFT entry 114.8274 on 2018-05-22 equals that bar's open 114.7700 x 1.0005, so the fill is next-bar open
  plus 5 bps slippage with zero commission.
- Artifacts per run: equity.csv, trades.csv, fills.jsonl, positions.csv, target_positions.csv, metrics.csv,
  validation.json, run_card.json, risk_xray.{json,md}, rebalance_notes.{json,md}.
- Unit tests: 586 passed in 14 s. Subset: tests/test_base_engine.py, test_global_equity_engine.py, test_metrics*.py,
  test_validation*.py, test_price_limit_lookahead.py, test_backtest_runner_security.py,
  test_walkforward_empty_training_regressions.py, test_bench_multiple_testing.py, tests/quantlib/test_{multipletesting,crossvalidation}.py

## 3. Review
License: MIT (LICENSE, "Vibe-Trading Contributors"); NOTICE credits HKUDS. Bundled Qlib factor definitions are
Apache-2.0 (agent/src/factors/zoo/qlib158/NOTICE). Alpha101/GTJA191 are formula reimplementations. Fonts are OFL.
Layout: agent/backtest/ is the engine (pure pandas/numpy, importable without the agent stack). agent/src/ is the agent:
  agent loop, about 80 tools in agent/src/tools/, swarm, live/, trading/connectors/, quantlib/, governance/, strategy_discovery/.
  The top-level tools/ folder holds only CI gates and scripts.
Engine: agent/backtest/runner.py (config.json + code/signal_engine.py → loader → engine),
  agent/backtest/engines/base.py (2.4k lines; bar-by-bar loop), engines/global_equity.py (US/HK/CA/UK rules), plus
  china_a, crypto, futures, forex, options_portfolio, composite (cross-market).
Fill/cost model:
  - Signals are target weights in [-1, 1]. _align() shifts each symbol by one bar on its own calendar
    (base.py ~L330-345). Fills happen at the next bar's open (execution_open), so there are no same-bar fills.
  - Slippage is a fixed fraction of price (US 5 bps, HK 10 bps). US commission is 0; HK has stamp duty, levies and
    commission; UK has SDRT; CN has T+1 and price-limit bands derived from the prior close (lookahead-safe).
  - Weights are normalised to gross ≤ 1. Modes are hold or rebalance, with an optional drift tolerance.
    Opens are scaled so commission cannot overdraw cash.
  - Gaps: no intrabar stops or targets, no volume or participation limits, no spread model, no PDT rule, no
    session or end-of-day flatten logic.
Metrics: agent/backtest/metrics.py: return, annual return, Sharpe, Sortino, Calmar, max drawdown, win rate, profit
  factor, turnover, excess return, information ratio, tracking error, per-symbol and per-exit-reason stats, and
  bars-per-year by interval and source. Plus risk_xray.py (HHI, effective N) and run_card.py (provenance).
Validation, agent/backtest/validation.py (enabled via config["validation"]):
  - monte_carlo_test shuffles the order of trade PnLs. Sharpe barely depends on order, so p_value_sharpe is close
    to meaningless; only the drawdown distribution tells you anything.
  - walk_forward_analysis is not walk-forward: it splits one fixed equity curve into N windows with no re-fitting.
    It is a sub-period consistency check.
  - bootstrap_sharpe_ci is an iid bootstrap. That is fine but it ignores autocorrelation (no block bootstrap).
  The stronger tools are in agent/src/quantlib/:
  - multipletesting.py (586 lines; numpy/pandas/scipy only): PSR, expected max Sharpe, deflated Sharpe,
    Benjamini-Hochberg FDR, and PBO via CSCV.
  - crossvalidation.py (636 lines; numpy/pandas only): purged/embargoed K-fold, purged walk-forward splits,
    combinatorial purged CV, and detect_boundary_leakage.
  - Also performance.py, risk.py, var_backtest.py and impact.py.
Strategy generation: yes, an LLM writes code/signal_engine.py through its write_file tool. backtest_tool.py runs it in
  a subprocess (agent/src/core/runner.py) with a timeout, a scrubbed env, an ephemeral HOME and an allowed-run-root
  check. runner.py runs an AST denylist over code reachable from SignalEngine (no socket/subprocess/eval/exec/file
  writes). Its own comment admits it is "defense-in-depth, not a kernel-level guarantee" and lists the modules that
  still get through. There is no OS sandbox (no seccomp, container or network namespace), and the agent also has a
  bash_tool. Governance pieces: strategy_discovery/ (per-regime evidence from artifacts), governance/ledger.py
  (hash-chained, fsynced, append-only JSONL), hypotheses/, goal/.
Brokers and execution controls: agent/src/trading/connectors/ has about 19 brokers (alpaca, ibkr, robinhood, binance,
  okx, …) with a paper/live split per profile. Live orders pass through src/live/order_guard.py, which fails closed:
  signed mandate (live/mandate/model.py HardCaps: max order notional, max exposure, max leverage, max trades per day,
  allowed instruments, universe/market-cap/ADV filters, exclusions) → expiry → filesystem HALT sentinel
  (live/halt.py) → account binding → intent parse → position/balance read → check_mandate. Every decision is
  audited (live/audit.py).
Data and frequency: intervals 1m/5m/15m/30m/1H/4H/1D/1W/1M. US chain (loaders/registry.py): yahoo, stooq, sina,
  eastmoney, yfinance, tiingo, fmp, finnhub, alphavantage, longbridge, akshare, local. The local loader reads
  CSV/Parquet/DuckDB from ~/.vibe-trading/data-bridge/config.yaml. The engine treats intraday bars generically.

## 4. What CTBOT should borrow (skeptical)
CTBOT already has: research/backtest.py (intraday ORB/VWAP/overnight; next-bar-open fills, stop-first, PDT, per-side
bps cost, IS/OOS) and research/lab.py (daily allocation sim, deflated_sharpe(), walk_forward() with genuine yearly
re-selection, stress()). For CTBOT's use case these are already more realistic than Vibe-Trading's equity engine.
1. Borrow with MIT attribution: agent/src/quantlib/multipletesting.py
   - probability_of_backtest_overfitting (CSCV) and benjamini_hochberg. CTBOT has neither, and lab.run() already
     ranks many catalog strategies, which is exactly where PBO applies.
   - Its deflated_sharpe_ratio can serve as a cross-check for lab.deflated_sharpe.
   - Dependencies: scipy.stats.norm only, and it could be swapped to statistics.NormalDist.
   - Copy the file plus its tests (tests/quantlib/test_multipletesting.py), keeping the copyright and license header.
2. Optionally borrow crossvalidation.purged_walk_forward_splits and detect_boundary_leakage, but only if CTBOT starts
   fitting models with multi-day labels. Today's fixed rules don't need purging.
3. Ideas only, no code:
   - Lookahead regression test: a "peek" signal must not profit (like smoke strategy B). Add it to CTBOT's tests.
   - evaluation_start_index (warmup_bars / evaluation_start_date): never grade warm-up bars.
   - run_card.json: record the data sources, config hash and code hash on every backtest.
   - Record dropped and rejected orders (plan_rejections, dropped_target_adjustments) instead of skipping silently.
   - Mandate-style HardCaps plus a filesystem HALT sentinel checked before every paper order are cheap, LLM-independent
     guards. Keep them paper-only; never import Vibe-Trading's live/ or connectors/.
   - Hash-chained append-only ledger (governance/ledger.py, 706 lines; the core idea is about 50) for the paper trade journal and strategy
     promotions.
   - Governed proposal flow: if CTBOT ever lets an LLM propose strategies, keep them as declarative parameters on
     catalog families, not generated Python. Vibe-Trading shows an AST denylist is not a sandbox.
4. Don't borrow: validation.monte_carlo_test (its Sharpe p-value is uninformative) or walk_forward_analysis
   (mislabelled), and not the engine itself (no stops, PDT or spread model; very heavy surrounding stack).
5. Worth checking in CTBOT (not changed): lab.simulate() computes the target from px[i], the close, and trades at the
   same px[i]. That assumes a market-on-close fill at the signal bar's close. Vibe-Trading and research/backtest.py
   both lag one bar. Consider a lag=1 option or a cost bump to confirm lab.py results survive.
