"""Trade post-mortems: why each trade was taken and how it ended (data/trade_log.jsonl).

At the buy, `entry_context` is stored on the position's bracket (persisted with the risk state):
the strategies, Claude's confidence and stop, the size multiplier, the sector, whether the
account was pulled back, and the market at that moment (the stock's and QQQ's move today, QQQ
against its trend average). While the position is open, `track` keeps its lowest and highest
bid. At the sale, `close_record` writes one JSON line: result, hold time, the result in R (the
move over the entry's risk a share), the most it was up and down, and for stop exits how far below the stop it actually sold (a gap or missing prices).
Positions opened before this log existed are logged without the entry context.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# A stop exit filled this far below its stop is flagged: the price gapped or the bot had no prices.
GAP_FLAG_PCT = 0.01


def _day_move(md, symbol: str, price: float | None) -> float | None:
    """Today's move: `price` against the last completed daily close."""
    try:
        bars = md.daily_bars(symbol, 1)
    except Exception:  # noqa: BLE001 - context is optional, never a blocker
        return None
    if not bars or not price or not bars[-1].close:
        return None
    return round(price / bars[-1].close - 1, 4)


def entry_context(md, symbol: str, fill_price: float, now: datetime, models: list[str], decision,
                  size_mult: float, sector: str, risk_off: list[str],
                  trend_symbol: str, trend_avg: float | None) -> dict[str, Any]:
    trend_q = None
    try:
        trend_q = md.quotes([trend_symbol]).get(trend_symbol)
    except Exception:  # noqa: BLE001
        log.debug("no %s quote for the entry context", trend_symbol)
    trend_px = trend_q.last if trend_q else None
    return {
        "opened_at": now.isoformat(),
        "models": models,
        "confidence": getattr(decision, "confidence", None),
        "claude_stop_pct": getattr(decision, "stop_loss_pct", None),
        "rationale": (getattr(decision, "rationale", None) or "")[:300] or None,
        "size_mult": size_mult,
        "sector": sector,
        "pulled_back": list(risk_off),
        "stock_day_move": _day_move(md, symbol, fill_price),
        "market_day_move": _day_move(md, trend_symbol, trend_px),
        "market_vs_trend": round(trend_px / trend_avg - 1, 4) if trend_px and trend_avg else None,
    }


def track(bracket: dict | None, bid: float | None) -> None:
    """Keep the lowest and highest bid seen while the position is open."""
    if not bracket or not bid:
        return
    bracket["lo"] = round(min(bracket.get("lo", bid), bid), 4)
    bracket["hi"] = round(max(bracket.get("hi", bid), bid), 4)


def close_record(symbol: str, bracket: dict, avg_cost: float, quantity: float, exit_price: float,
                 opened_at: datetime, now: datetime, reason: str | None) -> dict[str, Any]:
    ctx = bracket.get("ctx") or {}
    ret = exit_price / avg_cost - 1 if avg_cost else 0.0
    rec: dict[str, Any] = {
        "symbol": symbol,
        "models": ctx.get("models") or ([bracket["model"]] if bracket.get("model") else []),
        "opened_at": opened_at.isoformat(),
        "closed_at": now.isoformat(),
        "hold_hours": round((now - opened_at).total_seconds() / 3600, 2),
        "entry": round(avg_cost, 4),
        "exit": round(exit_price, 4),
        "quantity": quantity,
        "pnl": round((exit_price - avg_cost) * quantity, 2),
        "ret": round(ret, 4),
        "exit_reason": reason,
        "max_up": round(bracket["hi"] / avg_cost - 1, 4) if bracket.get("hi") and avg_cost else None,
        "max_down": round(bracket["lo"] / avg_cost - 1, 4) if bracket.get("lo") and avg_cost else None,
        # result in R: the move over the risk a share at entry (trader-exit's yardstick)
        "r_multiple": round((exit_price - avg_cost) / ctx["r0"], 2) if ctx.get("r0") else None,
        "context": ctx or None,
    }
    stop = bracket.get("stop")
    if reason in ("stop_loss", "trailing_stop") and stop:
        rec["stop"] = stop
        rec["below_stop"] = round(exit_price / stop - 1, 4)
        if rec["below_stop"] <= -GAP_FLAG_PCT:
            rec["note"] = f"{-rec['below_stop']:.1%} below its stop (price gap or no prices)"
    return rec


def append(path: Path, rec: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        log.exception("trade log write failed")


def read(path: Path, n: int = 50) -> list[dict[str, Any]]:
    """The last `n` post-mortems, newest first."""
    try:
        lines = path.read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
