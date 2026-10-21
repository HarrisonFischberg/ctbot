# Activity log

## 2026-10-20: reference-repo integration (research rigor, ranker, cost and safety controls)

- Evaluated TradingAgents, OpenAlice, Vibe-Trading and Qlib in separate environments outside the
  repo, offline with mocked data and LLMs (`docs/research/reference_repos/`); no accounts, keys,
  trades or paid calls.
- Added the factor library, walk-forward ranker, trial registry, PBO/FDR, lab `--lag`, shadow
  `ranker` model, review spend cap and log, unreviewed shadow ideas, stale-quote/HALT/duplicate
  guards, decision journal, outcome memory, optional debate, `ctbot-dayplan`, `ctbot-reconcile`.
- Live trading untouched: `LIVE_TRADING_ENABLED=false`, `tests/test_isolation.py` passes.
- Real-data backtests not run in this environment (price cache not available there); synthetic
  tests only. See the PR and `docs/RESEARCH_AND_OPERATIONS.md`.

## 2026-10-15: rename, open-source preparation, launch-readiness sweep

- Renamed the project to CTbot: package `rhbot` -> `ctbot`, commands `ctbot-*`, env `CTBOT_MODE`,
  config folder `~/.ctbot`, dashboard header, docs. Replica `sweep.py` over the files to be
  published: 0 hits for the old name or personal details.
- Added `LICENSE` (MIT, Rare Creatures LLC), `DISCLAIMER.md`, `TERMS.md`, `PRIVACY.md`,
  `SECURITY.md`, new `README.md`; old README moved to `docs/ARCHITECTURE.md`.
- Dashboard: footer disclaimer and links; skip link; Invest sheet focus handling (focus in, Tab
  wraps, Escape closes, focus returns to the opener); Settings → Disconnect Robinhood (deletes
  the local sign-in token).
- Contrast: rendered audit of every visible text element on all five tabs, light and dark
  (transitions disabled while measuring): failures fixed; final run 0 light, 0 dark after the
  last fix (selected-row tint).
- Keyboard check of the Invest sheet in the browser: opened with focus on the amount; Tab from
  the last control wrapped to the first; Escape closed it; focus returned to the Invest button.
- `.venv/bin/pytest -q`: 110 passed.
- Scans: logs and data for key/token values: none. Outbound hosts in code: Robinhood MCP,
  Anthropic (SDK), Collective2.
- `pip-audit --skip-editable` (2.10.1): no known vulnerabilities in installed packages.
