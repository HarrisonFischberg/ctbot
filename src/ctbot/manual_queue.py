"""Queue of dashboard clicks for the paper loop. Paper only.

The dashboard appends one JSON line per click to Settings.manual_orders_path. The paper loop
(the only process that trades) reads unprocessed orders, runs them through the risk manager
against a fresh quote, and appends one result line per order to manual_results_path. Two
append-only files keep a single writer per file, so the ledger is never written twice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ORDER_TTL_SECONDS = 600  # a click older than this is expired, not executed


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, default=str) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def enqueue(
    orders_path: Path, *, side: str, symbol: str, notional: float | None = None,
    idea_id: str | None = None, strategies: list[str] | None = None,
    stop_loss_pct: float | None = None, take_profit_pct: float | None = None,
) -> dict[str, Any]:
    order = {
        "id": uuid4().hex, "ts": datetime.now(timezone.utc).isoformat(), "side": side,
        "symbol": symbol.upper(), "notional": notional, "idea_id": idea_id,
        "strategies": strategies or [], "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
    }
    _append(orders_path, order)
    return order


def pending(orders_path: Path, results_path: Path) -> list[dict[str, Any]]:
    done = {r.get("order_id") for r in _read_jsonl(results_path)}
    return [o for o in _read_jsonl(orders_path) if o.get("id") not in done]


def record_result(results_path: Path, order: dict[str, Any], status: str, reasons: list[str],
                  fill: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "order_id": order["id"], "idea_id": order.get("idea_id"), "symbol": order.get("symbol"),
        "side": order.get("side"), "ts": datetime.now(timezone.utc).isoformat(),
        "status": status, "reasons": reasons, "fill": fill,
    }
    _append(results_path, result)
    return result


def read_orders(orders_path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(orders_path)


def read_results(results_path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(results_path)
