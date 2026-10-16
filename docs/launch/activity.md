# Activity log

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
