"""Measured Claude API spend: one JSON line per call in data/llm_usage.jsonl.

The decision engine (and the optional debate step) log `response.usage` here; the pipeline
refuses further paid reviews once today's estimated spend reaches MAX_CLAUDE_USD_PER_DAY.
Prices are Anthropic list prices per million tokens (2026-10); unknown models are priced as
Opus so the estimate errs high. Cache reads are billed at a tenth of the input price.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ctbot.market_hours import trading_date

PRICES_PER_M = {  # model: (input, output)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
DEFAULT = (5.0, 25.0)


def cost_usd(model: str, usage: Any) -> float:
    pin, pout = PRICES_PER_M.get(model, DEFAULT)
    get = (lambda k: usage.get(k) or 0) if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0) or 0)
    fresh = get("input_tokens") + 1.25 * get("cache_creation_input_tokens")
    cached = get("cache_read_input_tokens")
    return (fresh * pin + cached * pin * 0.1 + get("output_tokens") * pout) / 1e6


def record(path: Path, model: str, usage: Any, purpose: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    get = (lambda k: usage.get(k) or 0) if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0) or 0)
    usd = cost_usd(model, usage)
    row = {"ts": now.isoformat(timespec="seconds"), "day": trading_date(now).isoformat(), "model": model,
           "purpose": purpose, "input_tokens": get("input_tokens"), "output_tokens": get("output_tokens"),
           "cache_read_input_tokens": get("cache_read_input_tokens"), "usd": round(usd, 6)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return usd


def spent(path: Path, day: date) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("day") == day.isoformat():
            total += float(row.get("usd", 0))
    return total


def summary(path: Path) -> dict[str, Any]:
    """Totals by day and purpose, for reports and the final cost table."""
    by_day: dict[str, float] = {}
    by_purpose: dict[str, float] = {}
    calls = 0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            calls += 1
            by_day[row["day"]] = by_day.get(row["day"], 0.0) + row["usd"]
            by_purpose[row["purpose"]] = by_purpose.get(row["purpose"], 0.0) + row["usd"]
    days = len(by_day) or 1
    return {"calls": calls, "total_usd": round(sum(by_day.values()), 4),
            "avg_usd_per_active_day": round(sum(by_day.values()) / days, 4),
            "by_purpose": {k: round(v, 4) for k, v in by_purpose.items()}}
