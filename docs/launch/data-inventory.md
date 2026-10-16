# Data inventory

Rare Creatures LLC (the publisher) receives no data from any copy of CTbot. Everything below
happens on the user's own computer or between it and services the user connects.

| Data | Purpose | Collected at | Stored / sent to | Retention | Deletion |
|---|---|---|---|---|---|
| Anthropic API key | Claude reviews | `.env` or Settings tab | Local: `.env` / `~/.ctbot/settings.json` (0600). Sent to Anthropic as auth | Until removed | Settings → Remove; edit `.env`; revoke at Anthropic |
| Collective2 API key (optional) | Read-only strategy stats | `.env` or Settings | Local; sent to Collective2 as auth | Until removed | Settings → Remove; revoke at Collective2 |
| Robinhood account number + nickname | Live-gate account check (live is disabled); last 4 digits shown on dashboard | `.env` or Settings | Local only; never sent anywhere | Until removed | Settings → Remove; edit `.env` |
| Webull App Key + App Secret (optional) | Read-only candles (stock chart, daily-bar backup) | `.env` or Settings | Local: `.env` / `~/.ctbot/settings.json` (0600). Key sent to Webull; the secret only signs requests and is never sent | Until removed | Settings → Remove; delete the app at Webull |
| Webull 2FA token (optional) | Webull OpenAPI sign-in, when the Webull app has 2FA on | Settings → Connect Webull, approved in the Webull app | `~/.ctbot/webull_token.json` (0600); sent to Webull | Until disconnected; Webull drops it after 15 days unused | Settings → Webull → Disconnect |
| Robinhood OAuth token | Read-only market data | Robinhood sign-in (browser) | `~/.ctbot/robinhood_oauth.json` (0600); sent to Robinhood MCP | Until disconnected / expired | Settings → Disconnect; revoke in Robinhood |
| Paper ledger, positions, fills | Paper account | Paper bot | `data/` | Until deleted | Delete `data/` |
| Trade ideas + AI reviews | Results, scoring | Paper bot | `data/ideas.jsonl`; idea text sent to Anthropic for review | Until deleted | Delete `data/`; Anthropic per its policy |
| Market data cache | Backtests, performance | Bots, research commands | `data/bars_cache/` | Until deleted | Delete folder |
| Logs | Troubleshooting | All processes | `logs/` (no keys; verified by scan 2026-10-15) | Until deleted | Delete `logs/` |
| Dashboard preferences | Theme, layout, tab, sort, period | Browser | Browser localStorage on `127.0.0.1:8787`, keys `ctbot.*` | Until cleared | Clear site data |

Sent to Anthropic per review: symbol, strategy signals, prices/indicators, market context
(index moves, earnings, analyst ratings), paper equity/cash/positions. No account number,
nickname, name or email. Cookies: none.
