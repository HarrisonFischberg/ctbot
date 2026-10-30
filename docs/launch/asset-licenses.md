# Asset and dependency licenses

## Assets

| Asset | Source | License |
|---|---|---|
| Dashboard code, styles, icons (inline SVG paths) | Written for this project | MIT (this repo) |
| Fonts | System fonts only (`-apple-system`, `ui-serif`, `ui-monospace` stacks); none bundled or downloaded | Not distributed |
| Screenshots in `docs/images/` | Captured from CTbot's own dashboard with made-up demo data (generated prices, paper account, no real account) | MIT (this repo) |
| Chart palette | Validated default palette from the dataviz skill used during development; colour values only | Not copyrightable values |

## Third-party code and ideas (2026-10-20)

| Source (commit) | License | What CTbot uses | Obligation |
|---|---|---|---|
| microsoft/qlib (5435523) | MIT | Alpha158 factor formulas and TopkDropout logic, rewritten in pandas/numpy (`research/factors.py`, `research/ranker.py`) | Copyright notice kept in the module docstrings |
| HKUDS/Vibe-Trading (097b29f) | MIT | Ideas only: PBO/false-discovery checks, run cards, HALT file, hash-chained ledger; code rewritten (`research/validation.py`, `research/registry.py`) | Credited in docstrings (not required for ideas) |
| TauricResearch/TradingAgents (e8f5503) | Apache-2.0 | Ideas only: bull/bear debate, outcome memory (`decision/engine.py`, `decision/memory.py`); no code copied | None |
| TraderAlice/OpenAlice (87bc415) | AGPL-3.0 | Nothing copied; day-plan and thesis-check ideas reimplemented independently | None (no code used) |

No new Python dependency was added.

## Python dependencies (installed versions, 2026-10-15)

| Package | Version | License | Use |
|---|---|---|---|
| anthropic | 1.13.0 | MIT | Claude API client |
| mcp | 2.3.0 | MIT | Robinhood MCP client |
| httpx | 0.28.1 | BSD-3-Clause | HTTP |
| python-dotenv | 1.2.4 | BSD-3-Clause | `.env` loading |
| pydantic | 2.14.1 | MIT | Decision schema |
| numpy | 2.5.4 | BSD-3-Clause and others | Math |
| pandas | 3.0.6 | BSD-3-Clause | Data frames |
| arch (+ scipy, statsmodels) | 8.0.0 | NCSA (BSD-style); scipy BSD-3, statsmodels BSD-3 | SPA data-snooping test in research jobs |
| pytest (dev) | 9.1.1 | MIT | Tests |
| quantstats (optional `research`) | 0.0.86 | Apache-2.0 | Tearsheets |
| vectorbt (optional `research`) | 1.1.2 | **Apache-2.0 with Commons Clause** | Parameter sweeps |

vectorbt's Commons Clause forbids selling a product or service whose value derives
substantially from vectorbt. CTbot does not bundle it (optional extra, installed by the user)
and is free; anyone building a paid product on CTbot should drop the `research` extra or
review that clause. Transitive dependencies were not individually audited.
