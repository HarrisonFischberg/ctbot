"""Trading modes, set on the dashboard's Autopilot (Trading) tab. Paper only.

Saved in data/trading_modes.json and read every cycle; each change is appended with a timestamp
to data/trading_modes_log.jsonl.

* QQQ as market gauge (default on): the trend trigger of the pull-back (ctbot.risk_mode). Off,
  QQQ under its average no longer pulls the account back; the day-loss and drawdown triggers
  (RISK_OFF_DAY_LOSS_PCT, RISK_OFF_DRAWDOWN_PCT) are unchanged.
* Aggressive day trading (default off): orb, vwap_revert and orb_sip trade (overnight too if
  opted in), a symbol may be bought again after it was sold the same day, more ideas and Claude
  reviews fit (up to the tuning bounds, never lower than set), and every intraday position is sold
  at market `flatten_at` ET (15 minutes before an early close at the latest), so the account is in
  cash at the close. Systematic cores and positions with a planned multi-day exit are kept unless
  `flatten_all` is on. Every hard limit (loss halts, stop caps, spread, price, volume, no shorting,
  MAX_TRADING_CAPITAL, live trading off) is unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from ctbot import market_hours

INTRADAY = ("orb", "vwap_revert", "orb_sip")
SYSTEMATIC = frozenset({"trend_core", "trend_core_qld"})  # exempt from the flatten unless flatten_all
MAX_CANDIDATES, MAX_REVIEWS = 10, 100  # tops of tuning.KNOBS' bounds


@dataclass(frozen=True)
class TradingModes:
    qqq_gate: bool = True
    aggressive: bool = False
    overnight: bool = False            # aggressive: the overnight model trades too
    flatten_at: str = "15:45"          # ET
    flatten_all: bool = False          # also flatten the systematic cores and multi-day holds
    commission_per_order: float = 0.0  # $ per fill, taken off the P&L shown for aggressive mode


def _hhmm(s: str) -> time:
    hh, mm = (int(x) for x in s.split(":"))
    return time(hh, mm)


def validate(raw: dict[str, Any]) -> TradingModes:
    """Raises ValueError for anything outside the allowed ranges."""
    base = TradingModes()
    out: dict[str, Any] = {k: bool(raw.get(k, getattr(base, k)))
                           for k in ("qqq_gate", "aggressive", "overnight", "flatten_all")}
    flat = str(raw.get("flatten_at", base.flatten_at)).strip()
    try:
        ok = time(9, 30) < _hhmm(flat) < time(16, 0)
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("flatten time must be HH:MM ET between 09:31 and 15:59")
    out["flatten_at"] = _hhmm(flat).strftime("%H:%M")
    fee = float(raw.get("commission_per_order", base.commission_per_order))
    if not 0 <= fee <= 10:
        raise ValueError("commission per order must be between $0 and $10")
    out["commission_per_order"] = round(fee, 4)
    return TradingModes(**out)


def load(path: Path) -> TradingModes:
    try:
        return validate(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return TradingModes()


def log_path(path: Path) -> Path:
    return path.with_name("trading_modes_log.jsonl")


def save(path: Path, cfg: TradingModes) -> list[dict[str, Any]]:
    """Write `cfg` and log each changed field with a timestamp. Returns the logged changes."""
    old, now = asdict(load(path)), datetime.now().astimezone().isoformat(timespec="seconds")
    changes = [{"ts": now, "field": k, "from": old[k], "to": v} for k, v in asdict(cfg).items() if old[k] != v]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**asdict(cfg), "updated": now}))
    os.replace(tmp, path)
    if changes:
        with log_path(path).open("a") as fh:
            fh.writelines(json.dumps(c) + "\n" for c in changes)
    return changes


def changes(path: Path, n: int = 10) -> list[dict[str, Any]]:
    """The last `n` logged changes, newest first."""
    try:
        lines = log_path(path).read_text().splitlines()[-n:]
    except OSError:
        return []
    return [json.loads(ln) for ln in reversed(lines) if ln.strip()]


def traded_models(cfg: TradingModes) -> tuple[str, ...]:
    """Models aggressive mode switches to trading."""
    return INTRADAY + (("overnight",) if cfg.overnight else ()) if cfg.aggressive else ()


def flatten_due(cfg: TradingModes, now: datetime) -> bool:
    """Aggressive mode and at/after the flatten time (same lead before an early close)."""
    if not cfg.aggressive:
        return False
    lead = 16 * 60 - (_hhmm(cfg.flatten_at).hour * 60 + _hhmm(cfg.flatten_at).minute)
    return market_hours.minutes_to_close(now) <= lead


def to_flatten(cfg: TradingModes, positions, brackets: dict, now: datetime) -> list[str]:
    """Symbols to sell at market now. Intraday = opened today, not owned by a systematic core
    and with no planned exit on a later day (a swing extension doesn't count as planned)."""
    if not flatten_due(cfg, now):
        return []
    today = market_hours.trading_date(now)

    def kept(p) -> bool:
        b = brackets.get(p.symbol) or {}
        exit_at = b.get("exit_at")
        return (market_hours.trading_date(p.opened_at) != today or b.get("model") in SYSTEMATIC
                or bool(exit_at and not b.get("swings")
                        and market_hours.trading_date(datetime.fromisoformat(exit_at)) > today))

    return [p.symbol for p in positions if cfg.flatten_all or not kept(p)]
