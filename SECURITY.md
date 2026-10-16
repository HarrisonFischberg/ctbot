# Security

## Reporting a vulnerability

Please email hello@rarecreatures.app with the details rather than opening a public issue. We
will reply as soon as we can. There is no bug bounty.

## Keeping your keys safe

- Keys live in `.env` (ignored by git) or `~/.ctbot/settings.json` (file mode 0600). Never
  commit them, paste them into issues, or include them in screenshots or logs you share.
- The dashboard only listens on `127.0.0.1`, checks the Host and Origin of every request, and
  requires a per-run token for any change. Do not expose port 8787 to a network.
- If a key leaks, revoke it at the provider and create a new one; deleting it from a file or a
  git history is not enough.
- Real-money order code is disabled and unfinished (`src/ctbot/live/`). Paper processes cannot
  import it, and the test suite fails if code outside it references an order-placing tool.
