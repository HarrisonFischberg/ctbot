# Claims and evidence

| Claim (where) | Evidence |
|---|---|
| Paper trading only; never sends Robinhood an order (README, site) | `src/ctbot/live/robinhood_adapter.py`: `submit()` raises `NotImplementedError`; `READ_ONLY_TOOLS` allowlist in `market_data/robinhood_mcp_client.py`; `tests/test_isolation.py` |
| Paper process cannot import the real-money module (README, SECURITY) | `execution/isolation.py`, `live/__init__.py`; `tests/test_isolation.py` |
| Live needs mode + enable flag + confirmation phrase + account number and nickname (README) | `config.Settings.live_fully_authorized`; `tests/test_config.py` |
| Refuses any account but the named one (README) | `live/robinhood_adapter.verify_account`; `tests/test_isolation.py` |
| Hard $60 per real order (site) | `MAX_ORDER_NOTIONAL_USD = 60.0` in `live/robinhood_adapter.py` (applies only once live exists) |
| Dashboard local-only, rejects other sites, token for changes (README, SECURITY, PRIVACY) | `dashboard/server.py` Host/Origin/token checks; `tests/test_dashboard.py`, `tests/test_settings.py` |
| No cookies, no telemetry (README, PRIVACY) | No `document.cookie`/`Set-Cookie` in code; no analytics endpoints; network calls only to Robinhood MCP, Anthropic, Collective2 (`grep` of `src/`) |
| Logs contain no keys (PRIVACY) | Scan of `logs/` and `data/` for key and token values, 2026-10-15: no hits |
| Simulated 3/6/12-month returns (dashboard) | `ctbot-perf` (`research/perf.py`), labeled "simulated" with a not-a-promise note and worst drop |
| Strategy descriptions (dashboard) | `dashboard/plain.py` matches strategy code in `strategies/`; `tests/test_plain_and_perf.py` |
| Ranking model and other systematic models have no AI review and no API cost (dashboard) | `evidence["systematic"]` skips `engine.evaluate` in `pipeline._run_cycle`; `tests/test_ranker.py::test_shadow_strategy_matches_research_scoring` |
| Shadow ideas are saved without a paid review by default (COMMANDS, .env.example) | `REVIEW_SHADOW_IDEAS` in `config.py`; `tests/test_intraday.py::test_shadow_ideas_are_not_reviewed_by_default_and_spend_cap_stops_reviews` |
| macOS tested; Linux not tested (README) | Development and tests on macOS only |

Not claimed anywhere: profitability, beating the market, security certification, legal
compliance, or affiliation with Robinhood, Anthropic or Collective2.
