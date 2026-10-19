"""Dashboard assistant ("Bub"): explains what the paper bot is doing and proposes bounded changes.

  context.py   compact, secret-free snapshot of the bot's own files for the model
  actions.py   the allowlisted actions: validation, proposals, apply and revert
  audit.py     append-only data/assistant_audit.jsonl
  chat.py      Claude client (tool use for proposals), conversations, daily message cap

The model can only *propose*. A change happens when the person clicks Apply on the dashboard,
and the server re-validates it then. Paper only: nothing here imports ctbot.live, and no action
can reach the live flags, the Robinhood account settings or the money and loss caps.
"""
