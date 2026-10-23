# CTbot: notes for coding agents

CTbot (Creature Trading Bot) is a local, paper-only trading bot with a loopback dashboard.

## Safety boundaries
- Never call Robinhood order tools (`place_*`, `cancel_*`, `review_*_order`, `exercise_option`).
  Market data uses the read-only allowlist in `src/ctbot/market_data/robinhood_mcp_client.py`.
- Keep `LIVE_TRADING_ENABLED=false`; do not weaken the paper/live isolation (`execution/isolation.py`,
  `live/`, `tests/test_isolation.py`).
- Never print, log or commit keys from `.env` or `~/.ctbot/`. Tests must never echo a key value.
- `.claude/skills/trader-*` is the day-trader skill pack (see `.claude/skills/README.md`). Never use
  `/trader-broker` to connect a broker or place an order here; read-only tickets only.

## Verify
```bash
.venv/bin/pytest -q
```

## Records
- Launch-readiness checklist and evidence: `docs/launch/`
- Commands: `docs/COMMANDS.md` · Architecture: `docs/ARCHITECTURE.md`
- Legal drafts (need legal review): `DISCLAIMER.md`, `TERMS.md`, `PRIVACY.md`
