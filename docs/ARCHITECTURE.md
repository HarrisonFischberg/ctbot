# CTbot architecture

Multi-strategy AI trading agent. Pluggable strategies (momentum, mean reversion,
congressional trades, insider/institutional activity, copy-trading, unusual options
flow) emit signals; an ensemble ranks per-symbol candidates; Claude reviews each
candidate; a deterministic risk manager can veto; an executor (paper simulator, or
later Robinhood) acts.

**Status: scaffold. All execution is paper. Live execution is not implemented and disabled.**

## Flow

```
providers/ ──AltDataEvent──┐
market_data/ ──bars/quotes─┼─> strategies/ ──Signal──> aggregation/ ──Candidate──┐
                           │                                                      │
                           │     LONG:  decision/ (Claude) ──Decision──> risk/ ───┼─> executor
                           │     EXIT:  risk/ (no Claude; exits reduce risk) ─────┘   (paper | live)
```

## Layout

```
config/
  universe.txt                 symbols for price-based strategies
  strategies.toml              per-strategy enabled / weight / params
data/events/sample_events.json clearly-fake fixture events for paper runs
src/ctbot/
  config.py                    Settings.from_env(); live needs 3 separate flags
  models.py                    Bar, Quote, AltDataEvent, Signal, Candidate, Decision, Order, Fill
  pipeline.py                  one cycle; executor injected
  net.py                       guarded HTTP for public sources: timeout, retry, circuit breaker
  market_data/                 price data: CSV (offline), Robinhood MCP read-only
    tradingflow_client.py      TradingFlow MCP (API key): allowlisted read tools, circuit breaker
    webull_client.py           Webull OpenAPI (App Key/Secret, HMAC-signed): allowlisted market-data paths
    failover.py                Robinhood daily bars with Webull, then TradingFlow, as backup sources
    context.py                 review context + earnings guard (Robinhood, then public extras)
    tradingview.py             public TradingView scanner: earnings dates, technical rating
    news.py                    Marketaux news sentiment (optional key; numbers only)
    astral_client.py           Hey Astral MCP (OAuth, ctbot-astral-login): research-read allowlist only
    astral_news.py             Astral news labels for reviews: counts per event type, no headlines
    sec_edgar.py               SEC EDGAR (SEC_USER_AGENT): 8-K item codes + last reported quarter for
                               reviews; bankruptcy / delisting / non-reliance 8-Ks block entries
  providers/                   alt data, normalized to AltDataEvent
    base.py                    AltDataProvider protocol
    registry.py                build by name; one failing provider never blocks others
    json_file.py               offline events (paper/tests)
    quiver.py                  Quiver Quantitative stub (congress, Form 4, 13F)
    unusual_whales.py          Unusual Whales stub (options flow, congress)
    robinhood_mcp.py           Robinhood MCP stub (get_politician_trades, ...)
    cboe_options.py            free Cboe delayed chains -> unusual-volume OPTIONS_FLOW events
    tradingflow_options.py     TradingFlow trade-level flow (ask/bid side) -> OPTIONS_FLOW events
    house_clerk.py             free House PTRs (pdftotext) -> CONGRESS_TRADE events
    astral.py                  Hey Astral Form 4 open-market trades -> INSIDER_TRADE events
  strategies/
    base.py                    Strategy protocol + StrategyContext
    registry.py                load strategies.toml, build enabled strategies
    momentum.py                cross-sectional, vol-adjusted, trend-filtered
    mean_reversion.py          z-score dips with short half-life, trend-filtered
    event_cluster.py           congressional / institutional / copy_trading
    options_flow.py            net bullish premium of large prints
    trend_template.py          stage-2 trend template (maverick-mcp supply/demand screen)
  aggregation/ensemble.py      weighted agreement score; min agreeing strategies; caps Claude calls
  analysis/stats.py            z-score, returns, SMA, vol, half-life
  analysis/regime.py           SPY trend/volatility regime label for reviews
  analysis/setups.py           daily-close chart setups (20d breakout, bull flag, MA20 reclaim, oversold)
  decision/                    Claude review -> JSON Decision; fails closed; memory.py (settled paper
                               results per model, as-of); optional bounded for/against debate
  contracts.py                 MarketSnapshot / StrategyResult / ProposedOrder / ExecutionOutcome /
                               CycleSummary; append-only journal (data/journal.jsonl)
  llm_costs.py                 measured Claude spend per call; daily $ cap
  dayplan.py                   ctbot-dayplan (premarket plan, explicit no-trade) / ctbot-reconcile (EOD)
  assistant/                   dashboard assistant (Bub): context, allowlisted proposals, audit, Claude chat
  thoughts.py                  "Bubs Thoughts": deduped, rate-limited notes on the market and on
                               choices, and what the bot saw just before a buy (data/bub_thoughts.jsonl);
                               notes only, never changes a trade
  tuning.py                    assistant-tunable knobs (~/.ctbot/tuning.json), bounds and denylist
  autopilot.py                 Autopilot: every enabled model may trade; benching; trailing stops
  variants.py                  side-by-side paper books (config/variants.toml), run in the paper process
                               after the main book with shared market data and Claude verdicts
  track_record.py              size multiplier per strategy from its last 30 days of paper results
  backtest_check.py            live average per trade vs config/backtests.toml ("Vs backtest" column)
  risk_mode.py                 pull back after losses: no borrowing on a bad day, drawdown or QQQ downtrend
  trading_modes.py             Trading tab modes (data/trading_modes.json): QQQ-gauge toggle, aggressive
                               day trading with the 15:45 ET flatten
  trade_log.py                 per-trade post-mortems (data/trade_log.jsonl)
  library/                     12 published rule families x 2 universes (lib_*), traded only if
                               ctbot-library-research admits them
  watchdog.py, notify.py       heartbeat ping (HEARTBEAT_URL) and ntfy phone messages (NOTIFY_URL)
  risk/manager.py              check_entry / check_exit
  execution/                   OrderExecutor protocol + paper-process import guard
  paper/                       PaperBroker + `ctbot-paper` entrypoint
  live/                        RobinhoodLiveExecutor (guarded, not implemented) + `ctbot-live`
  research/timemachine.py      seeded replays of the rest of a session from 5-min bars (ctbot-timemachine;
                               intraday reviews get the odds)
  research/                    backtest (5-min), lab (daily portfolios, --lag), factors (Alpha158 subset),
                               ranker (purged walk-forward + TopkDropout), registry (trial log, holdout
                               lock), validation (PBO, FDR); see docs/RESEARCH_AND_OPERATIONS.md
tests/                         isolation, config, risk, strategies/ensemble/providers
.claude/skills/                agent skills: the 11 trader-* day-trader skills, find-skills and 12 vetted
                               trading skills (README maps each to the bot; the bot doesn't read them)
deploy/                        Ubuntu server: setup-server.sh, tailscale.sh, update.sh, systemd units
                               (paper, dashboard, watchdog/research/update timers); docs/CLOUD_HOSTING.md
```

## Extending

- **New data vendor**: class with `name`, `supports`, `fetch(kinds, since)` returning
  `AltDataEvent`s; add to `providers/registry.PROVIDER_FACTORIES`; list in `ALT_DATA_PROVIDERS`.
- **New strategy**: class with `name`, `event_kinds`, `generate(ctx)` returning `Signal`s;
  add to `strategies/registry.STRATEGY_CLASSES`; add a table in `config/strategies.toml`.

## Paper / live isolation

1. Separate packages and entrypoints, so separate OS processes.
2. `ctbot-paper` calls `install_paper_guard()` before any other import: importing
   `ctbot.live` then raises, and live flags are forced off in that process.
3. `RobinhoodLiveExecutor` refuses inside a paper process, and unless `CTBOT_MODE=live`,
   `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_CONFIRM` all match. Hard $60/order cap.
4. Tests fail if code outside `ctbot/live/` imports it or names an order-placing MCP tool.
5. Providers and market data reference read-only MCP tools only.

## Caveats

- **Disclosure lag**: congressional trades can be disclosed up to 45 days late; 13F up
  to 45 days after quarter end. Much of the edge may be priced in by then.
- **Options flow is noisy**: many large prints are hedges or spreads, not directional bets.
- **Small cash accounts**: no shorting (bearish signals only exit held longs); no day-trade count limit since FINRA's
  intraday margin standard replaced PDT on 2026-06-04 (`PDT_RULE=true` restores 3 per 5 days); Claude API cost per candidate can exceed expected edge.
- **Paper margin** (`PAPER_MARGIN`, 1 to 2): buys may borrow up to 2x equity with $2,000+ equity,
  paused while equity is under 25% of positions; no margin interest is simulated. Default 1 = cash only.
- **Paid data**: Quiver and Unusual Whales need subscriptions and API keys.
- **Free public data is unofficial**: the TradingView scanner and Cboe's delayed chains are
  public web endpoints with no API contract or SLA; they can change, rate-limit or vanish.
  Each source sits behind `net.HttpSource` (one retry, then a 10-minute pause after repeated
  failures) and every caller treats a miss as "no data". Check each site's terms before any
  use beyond personal research. Cboe options flow infers direction from the last trade vs the
  mid, so it is much weaker evidence than a paid sweep/block feed.
- **TradingFlow is a second source, not a replacement**: with `TRADINGFLOW_API_KEY` set it
  backs up Robinhood's completed daily bars and feeds `tradingflow_options`. Its candles run
  about 15 minutes behind and are not split-adjusted, so quotes and intraday bars stay
  Robinhood-only (no Robinhood quote, no entry), and a daily series is cut at any jump its own
  previous close cannot explain. Its options flow is still flow: hedges and spreads look like
  directional bets.
- **Robinhood MCP is reachable via Claude, not plain Python**: live wiring and the
  read-only MCP data paths must go through the Claude API MCP connector.

## Running

Needs Python 3.11+ (`tomllib`). On macOS: `brew install python@3.12`.

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
ctbot-paper            # one cycle; --loop to repeat
```

### Daily paper trading

```
.venv/bin/ctbot-robinhood-login     # once: browser sign-in for read-only market data
.venv/bin/ctbot-astral-login        # once, optional: Hey Astral sign-in (needs ASTRAL_MCP_URL)
./run_ctbot.sh                       # dashboard + paper bot (5-min cycles), Ctrl+C stops both
```

The dashboard is at http://127.0.0.1:8787 (loopback only). Clicks there only queue
**paper** orders; the paper bot executes them after re-running every risk check.

What a paper day does (all settings in `.env` / `config/strategies.toml`):

| Piece | Behavior |
|---|---|
| Universe | 88 liquid stocks/ETFs (`config/universe.txt`), fractional-tradable on Robinhood |
| Intraday models | `orb` (opening-range breakout), `vwap_revert` (dip below VWAP), `overnight` (close-to-open). Each carries its own stop, target and exit time |
| Daily models | `momentum`, `mean_reversion` (need 2 to agree) |
| Review | Claude reviews each candidate once per day with market context (time, SPY/QQQ, earnings, analyst targets); `MIN_CLAUDE_CONFIDENCE`; at most `MAX_CLAUDE_REVIEWS_PER_DAY` |
| Hard rules | size cap, `MAX_OPEN_POSITIONS`, $ cap, daily-loss halt, drawdown kill switch, day-trade budget (only with `PDT_RULE`), spread/liquidity, never hold through earnings |
| Exits | model stop/target/time; winners up `SWING_MIN_PROFIT_PCT` near exit are held to the next close (max `SWING_MAX_DAYS`); a model is paused for the day after `MODEL_MAX_LOSING_STREAK` losses |
| Results | every fill is tagged with its model; the dashboard's Models panel is the scoreboard |

### Research (read-only)

```
.venv/bin/ctbot-fetch-bars --interval 5minute --since 2026-02-15   # cache bars
.venv/bin/ctbot-backtest                                            # IS/OOS backtest
.venv/bin/ctbot-c2-rank && .venv/bin/ctbot-c2-track                 # Collective2 screen + forward tracking
```

A launchd job (`com.ctbot.c2-track`) refreshes the bar cache and Collective2 tracking on
weekdays at 07:00 and 15:00 local time.
