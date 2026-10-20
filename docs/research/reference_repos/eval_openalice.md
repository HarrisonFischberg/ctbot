# OpenAlice evaluation for CTBOT (commit 87bc4152, v0.95.0-beta)

**Verdict:** borrow the ideas and rewrite them in Python from scratch. Don't add it as a component. The license is AGPL-3.0-only (`LICENSE`, `package.json` "license"), so no code can go into MIT CTBOT. Running it beside CTBOT as a separate service is legal but brings little: it's a 1.2 GB Node/Electron agent stack built around coding-agent CLIs, crypto venues and IBKR/Alpaca/Longbridge, and it has no Robinhood support.

## 1. Install (headless, in its own directory)
- `git clone /home/claude/refs/OpenAlice /home/claude/refs/envs/openalice` (the source clone was left untouched)
- `pnpm install --ignore-scripts --frozen-lockfile` with pnpm 11.7.0 via corepack and node 22.22. Took 23.5s, node_modules is 1.2 GB. No Electron or node-pty postinstall ran.
- To start the server I also built only the core libraries: `npx turbo run build --filter=@traderalice/{uta-protocol,ibkr,guardian-runtime,connector-protocol,opentypebb,update-lifecycle}`. 6/6 succeeded in 19s. Desktop, UI and broker packs were not built.

## 2. Smoke tests (no keys, no network trading, no LLM calls)
All runs used `OPENALICE_TEST_HOME=<scratchpad>/oa-home`. `vitest.setup.ts` points the whole OpenAlice home, git config and launcher root at temp directories.
1. `npx vitest run --project node services/uta/src/domain/trading src/workspaces/schedule src/core/schedule-expr.spec.ts services/uta/src/http`
   **32 files, 745 tests passed in 15.5s.** Covers TradingGit, order-entry, the broker adapters (Alpaca, CCXT, IBKR, Longbridge, LeverUp), cost-basis, position math, the schedule scanner and marker store, cron/at/every evaluation, and the HTTP trading routes. Broker SDKs and HTTP are stubbed with vi mocks and fixtures.
2. `npx vitest run --project node tests/integration/uta-simulation`
   **9 files, 240 tests passed in 9.3s.** Covers guards, UnifiedTradingAccount, the order-sync poller, snapshots and trading tools. All 9 files run against `MockBroker`, an in-memory exchange with Decimal math, not a real broker.
3. `npx vitest run --config vitest.integration.config.ts tests/integration/trading-approval tests/integration/alice-uta`
   **2 files, 21 tests passed.** Covers the stage → commit → push → sync lifecycle and the approval HTTP path, using MockBroker.
4. Headless trading service (UTA):
   `OPENALICE_HOME=<scratch>/oa-uta-home OPENALICE_UTA_PORT=47999 NODE_OPTIONS=--conditions=openalice-source timeout 25 tsx services/uta/src/main.ts`
   It started with no credentials. It applied config migrations 0039–0044, started the 15-minute snapshot scheduler and the 10-second order-sync poller, and listened on 127.0.0.1:47999.
   - `GET /api/trading/uta` returned `{"utas":[]}`.
   - `GET /api/trading/equity` returned all zeros.
   - `POST /api/trading/uta/nope/wallet/push` returned `{"error":"Account not found"}`.
   No accounts were configured, so no broker connected. Keyless crypto data sources are off unless enabled in config.
   Side effect: on first run it writes `sealing.key` and AES-GCM-sealed `accounts.json` into the scratch home. The scratch home is only under the scratchpad.
- Not run: the full Alice server with UI, agent CLIs and workspaces, the Electron app, and the `test:live:*` and `external-readonly` test lanes.

## 3. Review (paths relative to the repo root)
- **Reviewable trading workflow ("Trading-as-Git"):** `services/uta/src/domain/trading/git/TradingGit.ts`, with the design notes in `services/uta/src/domain/trading/README.md` (written in Chinese).
  - The flow is `add` (stage) → `commit(msg)` → `push(expectedPendingHash)` or `reject(reason, expectedPendingHash)`. Commit gives the batch an 8-character hash and links it to its parent commit.
  - Staging is locked while a commit awaits approval, and a write already in flight blocks new ones.
  - Push and reject must quote the pending hash, so a stale approval gets a 409 `PENDING_HASH_CONFLICT`. This is a good guard against the batch changing between review and approval.
  - Push only reports `submitted` or `rejected`. A separate `sync` step records fills from the broker; see `order-sync-poller.ts`.
  - Each commit stores its operations, the results and a state snapshot. The history persists to `data/trading/<acct>/commit.json` (`git-persistence.ts`).
- **Approval wall:** `src/tool/trading.ts`, around L800–850.
  - The agent's push tool does not execute unless the setting `agent.allowAiTrading` is on. Otherwise it tells the user to approve in the Web UI.
  - Weakness 1: the approval wall lives only in the agent tool layer. The trading service's HTTP API (`services/uta/src/http/routes-trading.ts` L503, plus the one-shot `/wallet/place-order` at L594 built on `order-entry.ts`) has no auth beyond binding to loopback. Any local process can push.
  - Weakness 2: one AI setting can turn approval off entirely.
- **Guards:** `services/uta/src/domain/trading/guards/`. Files: `guard-pipeline.ts`, `max-position-size.ts`, `cooldown.ts`, `symbol-whitelist.ts`, `registry.ts`.
  - Each guard gets a read-only context of the operation, positions and account, and returns either null or a rejection reason. The guards are configured per account.
  - Gap 1: the cooldown guard records the trade time when it checks, even if a later guard or the broker rejects the order.
  - Gap 2: max-position-size allows a quantity-based order on a new symbol because it cannot estimate the order's value.
  - CTBOT's `risk/manager.py` should keep its stricter fail-closed behaviour.
- **Broker adapters:** `services/uta/src/domain/trading/brokers/`. Includes Alpaca (paper/live), CCXT (Bybit, Bitget, Hyperliquid…), IBKR (its own TWS wire port in `packages/ibkr`), Longbridge, LeverUp, `mock/MockBroker.ts`, and an `IBroker` interface in `types.ts`.
  - Some adapters ship as installable "broker packs" (`packages/uta-broker-*`).
  - There is no Robinhood adapter.
- **Persistent research workspaces:** `src/workspaces/` (workspace-registry, workspace-creator, git-service.ts) and the templates in `src/workspaces/templates/{chat,auto-quant-v2,auto-prediction}`.
  - Each workspace is a durable git-tracked directory where a native coding-agent CLI (Claude Code, Codex, pi…) runs. Alice launches the CLI and injects context; the CLI runs the model loop itself.
- **Scheduled investigations:** see `docs/workspace-issues-and-scheduling.md`.
  - Each `<ws>/.alice/issues/<id>.md` is a markdown file whose YAML frontmatter can include `when: {kind: at|every|cron, timezone}`.
  - `src/workspaces/schedule/scanner.ts` ticks about once a minute and starts a headless agent run for any issue that is due.
  - Firing state is a single last-fired timestamp per issue (`marker-store.ts`), written only after a successful dispatch. Missed cron slots are either caught up or skipped, set by `catchUp`.
  - Schedules are evaluated by a pure function (`src/core/schedule-expr.ts`) that takes an explicit time instead of reading the clock. Internal timers such as the snapshot scheduler use `src/core/pump.ts`.
  - Results go to the Inbox (`src/core/inbox-store.ts`).
- **Thesis monitoring:** prose only. `default/skills/build-thesis/SKILL.md` asks the agent to write `<theme>/notes/<TICKER>.md` with the claim, what must be true, and disconfirming signals, and to hand those signals to a "position monitor". That monitor does not exist in code; I grepped and found no deterministic invalidation checker.
- **Memory:**
  - Append-only JSONL event log with a ring buffer: `src/core/event-log.ts`.
  - Curated entity watchlist that notes link to with `[[name]]`, plus backlinks: `src/core/entity-store.ts`, `entity-backlinks.ts`.
  - Inbox: `src/core/inbox-store.ts`.
  - Account snapshots: `services/uta/src/domain/trading/snapshot/`.
  - Beyond these, memory is the workspace files themselves. There is no vector database.
- **Other good habits:**
  - Decimal arithmetic everywhere.
  - Strict tests that `MockBroker` must behave like real brokers.
  - Secrets sealed at rest (`src/core/sealing.ts`).
  - All storage is files, with no database.

## 4. Recommendations for CTBOT (clean-room Python, no code copying)
1. **Staged order log with approval:** `src/ctbot/execution/order_log.py`. Highest value.
   - Proposals from the decision engine, assistant or dashboard become a staged batch.
   - `commit(message)` produces a content hash (sha256 of ops + message + parent + timestamp).
   - Approval must quote that hash. A hash mismatch, or a batch older than the TTL (reuse `manual_queue.ORDER_TTL_SECONDS`), means reject.
   - Store everything in an append-only JSONL ledger: proposed/committed/approved/rejected/submitted/filled, with a risk verdict per operation and a state snapshot.
   - Unlike OpenAlice, enforce approval inside the paper loop, the single writer, not in a tool layer. Don't add an "AI may self-approve" switch, or keep it paper-only and default it off.
   - Keep push (submit to the paper simulator) separate from sync (fill confirmation).
2. **Thesis files with deterministic invalidation:** `research/theses/<TICKER>.yaml`.
   - Fields: claim, entry ref, `invalidate_if` rules from a closed set (e.g. close below level, drawdown over X% from entry, relative strength vs SPY below N over K days, earnings date passed, max holding period), `review_by` date and status.
   - A pure Python checker evaluates the rules against market data. When a thesis breaks, it stages an exit proposal into item 1 and never auto-executes it.
   - An LLM may draft the YAML, but only code decides what is true.
3. **Scheduled investigations:** a schedule spec (`at`/`every`/`cron` plus an explicit IANA tz, America/New_York) evaluated by a pure function that takes `now`.
   - Persist a single last-fired marker per job, written only after success, with an explicit catch-up policy. Gate jobs with `market_hours.py`.
   - The `croniter` package (MIT) can do the cron evaluation.
   - Each job writes a dated markdown note and a dashboard inbox entry. The thesis checker is just one scheduled job.
4. **Guard pipeline refactor:** small composable guards (read-only context in, reason or None out) inside `risk/manager.py`.
   - Record the cooldown only after a successful fill.
   - Fail closed when an order's value can't be estimated, which fixes OpenAlice's gap.
5. **Mock broker parity tests:** treat `paper/simulator.py` as the reference behaviour that any future adapter must match in tests.

**Separate service: not recommended.**
- It duplicates CTBOT's paper loop and dashboard.
- It needs node ≥ 22 and 1.2 GB of dependencies.
- It brings the AGPL network-use clause.
- Its trading service API has no auth.
- Its broker set doesn't match CTBOT's (no Robinhood).
- It is built around native agent CLIs that make paid LLM calls.

If anything, the only part worth reusing directly is the reference behaviour of `MockBroker`, and only as a spec to read. Do not use any of its code.
