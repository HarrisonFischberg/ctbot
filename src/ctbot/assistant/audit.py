"""Append-only log of every assistant proposal and what became of it: data/assistant_audit.jsonl.

One JSON line per event: proposed, rejected (failed validation), applied, failed, dismissed.
Lines are never rewritten; a revert is a new "applied" line whose `revert_of` names the line it
undoes. Every line is passed through safety.redact first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctbot.assistant.safety import redact
from ctbot.manual_queue import _append, _read_jsonl

MAX_MESSAGE_CHARS = 500


def record(path: Path, event: str, **fields: Any) -> dict[str, Any]:
    entry = {"id": uuid4().hex, "ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    if isinstance(entry.get("user_message"), str):
        entry["user_message"] = entry["user_message"][:MAX_MESSAGE_CHARS]
    entry = redact(entry)
    _append(path, entry)
    return entry


def read(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def find(path: Path, entry_id: str) -> dict[str, Any] | None:
    return next((e for e in read(path) if e.get("id") == entry_id), None)


def reverted_ids(entries: list[dict[str, Any]]) -> set[str]:
    return {e["revert_of"] for e in entries if e.get("event") == "applied" and e.get("revert_of")}


def recent(path: Path, n: int = 30) -> list[dict[str, Any]]:
    """Newest first, each applied change marked with whether Revert can still undo it."""
    entries = read(path)
    undone = reverted_ids(entries)
    out = []
    for e in reversed(entries[-max(n * 4, 200):]):
        if e.get("event") == "proposed":
            continue  # the outcome line says it all
        e = dict(e)
        e["reverted"] = e.get("id") in undone
        e["can_revert"] = bool(e.get("event") == "applied" and e.get("revertible") and not e["reverted"])
        out.append(e)
        if len(out) >= n:
            break
    return out
