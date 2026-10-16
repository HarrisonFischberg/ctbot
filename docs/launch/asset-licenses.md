# Asset and dependency licenses

## Assets

| Asset | Source | License |
|---|---|---|
| Dashboard code, styles, icons (inline SVG paths) | Written for this project | MIT (this repo) |
| Fonts | System fonts only (`-apple-system`, `ui-serif`, `ui-monospace` stacks); none bundled or downloaded | Not distributed |
| Screenshots in `docs/images/` | Captured from CTbot's own dashboard with demo data (anonymised account) | MIT (this repo) |
| Chart palette | Validated default palette from the dataviz skill used during development; colour values only | Not copyrightable values |

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
| pytest (dev) | 9.1.1 | MIT | Tests |
| quantstats (optional `research`) | 0.0.86 | Apache-2.0 | Tearsheets |
| vectorbt (optional `research`) | 1.1.2 | **Apache-2.0 with Commons Clause** | Parameter sweeps |

vectorbt's Commons Clause forbids selling a product or service whose value derives
substantially from vectorbt. CTbot does not bundle it (optional extra, installed by the user)
and is free; anyone building a paid product on CTbot should drop the `research` extra or
review that clause. Transitive dependencies were not individually audited.
