# Privacy

Last updated: 15 October 2026

**Draft for legal review.** This describes what the CTbot software does with data, based on
the code in this repository.

## Who this covers

CTbot is open-source software you run on your own computer. Rare Creatures LLC publishes the
code but runs no CTbot server, has no CTbot accounts, and receives no data from your copy of the
software: no analytics, no telemetry, no crash reports, no usage pings.

What the software stores stays on your computer. What it sends goes directly from your computer
to the services you connect, under those services' own privacy policies.

## What stays on your computer

| Data | Where | Why |
|---|---|---|
| Your API keys and Robinhood account number and nickname | `.env` in the project folder, or `~/.ctbot/settings.json` (readable only by your user) if saved from the dashboard's Settings tab | To call the services you connect |
| Robinhood sign-in token | `~/.ctbot/robinhood_oauth.json` (readable only by your user) | So you don't sign in on every run |
| Paper account: cash, positions, simulated fills | `data/` in the project folder | To run the paper account |
| Trade ideas, AI reviews, strategy settings and results | `data/` | To show results and score strategies |
| Cached market prices | `data/bars_cache/` | Backtests and the 3/6/12-month figures |
| Logs | `logs/` | Troubleshooting. Logs never contain your keys |
| Dashboard preferences: theme, card layout, open tab, table sorting | Your browser's local storage on `127.0.0.1` (keys starting `ctbot.`) | Remember how you like the dashboard |

The dashboard sets **no cookies** and loads no outside fonts, scripts or trackers. It only
listens on your own computer (`127.0.0.1`), and refuses requests from other websites.

## What is sent to other services

Only to services you configure, only from your computer:

- **Robinhood** (through its MCP server at `agent.robinhood.com`): read-only requests for quotes,
  price history, account and position details, earnings dates, analyst ratings and market scans.
  CTbot does not send orders to Robinhood. See Robinhood's privacy policy.
- **Anthropic** (Claude API), if you add a key: the trade idea being reviewed, with market
  context such as prices, recent moves, earnings dates and your paper account's cash and
  positions. Your API key authenticates the request. No account numbers or names are sent.
  See Anthropic's privacy policy and API terms.
- **Collective2**, if you add a key: read-only requests for public strategy statistics.

## How long it is kept

Until you delete it. Nothing is deleted automatically, except that the paper bot's dashboard
click queue expires after a short time.

## Deleting your data

Everything is on your computer, so you can delete all of it yourself:

1. **Saved keys:** dashboard → Settings → **Remove** next to each key, and remove them from
   `.env`.
2. **Robinhood sign-in:** dashboard → Settings → **Disconnect** (deletes the saved token). To
   remove CTbot's access completely, also revoke it in your Robinhood account settings.
3. **Paper account, results, prices and logs:** delete the `data/` and `logs/` folders.
4. **Everything:** stop CTbot, delete the project folder and `~/.ctbot`, and clear the
   `127.0.0.1:8787` site data in your browser.
5. **At each service:** to delete data a service holds (for example Anthropic API logs), use that
   service's own deletion or privacy request process. CTbot cannot do this for you.

Rare Creatures LLC holds no CTbot data about you, so there is nothing for us to delete. If you
email us, we keep your email only as long as needed to reply, and delete it on request.

## Children

CTbot is not for children. Brokerage accounts require users to be adults, and the software is
intended for people aged 18 or over. It collects no data for us from anyone.

## Changes and contact

Changes to this notice are published in this repository with a new date.
Questions: hello@rarecreatures.app (Rare Creatures LLC, Colorado, United States).
