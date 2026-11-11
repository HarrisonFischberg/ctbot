# CTbot: notes for coding agents

CTbot (Creature Trading Bot) is a local, paper-only trading bot with a loopback dashboard.

## Safety boundaries
- Never call Robinhood order tools (`place_*`, `cancel_*`, `review_*_order`, `exercise_option`).
  Market data uses the read-only allowlist in `src/ctbot/market_data/robinhood_mcp_client.py`.
- Keep `LIVE_TRADING_ENABLED=false`; do not weaken the paper/live isolation (`execution/isolation.py`,
  `live/`, `tests/test_isolation.py`).
- Never print, log or commit keys from `.env` or `~/.ctbot/`. Tests must never echo a key value.
- `.claude/skills/trader-*` is the day-trader skill pack (see `.claude/skills/README.md`).
  `/trader-broker` may place orders on a connected broker's **paper** account (Alpaca paper by
  default) only through its three gates: trader-risk PASS, the user's typed yes for that one order,
  and the user's Allow once on the tool card. Never switch it to a live account, never from a
  background task, and never route it through Robinhood (the rule above still holds).

## Coding style
Ponytail is installed (see `.claude/skills/README.md`); its rules load from here:
@.claude/ponytail-rules.md

## Verify
```bash
.venv/bin/pytest -q
```

## Records
- Launch-readiness checklist and evidence: `docs/launch/`
- Commands: `docs/COMMANDS.md` · Architecture: `docs/ARCHITECTURE.md`
- Legal drafts (need legal review): `DISCLAIMER.md`, `TERMS.md`, `PRIVACY.md`

## Model Routing and Sub-Agents

The main session is the orchestrator. It plans, makes judgment calls, edits small things itself, and merges results. Sub-agents live in `.claude/agents/`.

| Work | Agent | Model |
|---|---|---|
| Find files, search code, read logs/test output, summarize | `scout` | Haiku 5.5 |
| Well-scoped builds: bug fixes, features against a clear spec | `builder` | Sonnet 5.5 |
| Hard builds: migrations, performance, concurrency, crypto, under-specified work | `senior-builder` | Opus 5.5 |
| Independent check of any non-trivial code change | `reviewer` | Opus 5.5 |

- Delegate only when it pays: the work is parallel, read-heavy, or bigger than one context. A small task or a single chain of dependent steps stays inline.
- Every builder brief names the files, what done looks like, and the check to run (build or tests).
- Before reporting a non-trivial code change from a builder as done, run `reviewer` on the diff. Give it the task and the code, not your conclusion. Trivial changes (copy, one-liners, config) skip review.
- If a builder falls short: add a self-check first, then raise effort, then move up a model (builder -> senior-builder -> do it yourself).
