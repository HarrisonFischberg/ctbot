"""The 96 index dip rules, scored every night on what they would have done, paper research only.

A rule is one signal on SPY or QQQ at a close (8 signals) x one gate on QQQ's 170-day average
(any, above, below) x one hold (1 or 5 days): buy the next open, sell the close 1 or 5 days after
the signal. They were declared once, before any result was read (project study of 2026-11-18), and
the list never changes, so the count that the multiple-testing maths needs stays 96.

Every night `score` replays all 96 on the daily bar cache (no Claude call, no orders):
* history: 2000-2012 and 2013 to FORWARD_FROM, the two periods the study read. 2 of 96 passed both.
* forward: trades entered from FORWARD_FROM on, bars nobody had seen when the rules were written.
  This is the record that builds night by night and the only one that can promote a rule.

A rule is `winning` once it has MIN_TRADES forward trades over MIN_DAYS, a positive average and
positive excess over simply holding in the same gate, and its forward p-value survives
Benjamini-Hochberg at 10%: among the history passers if it is one of them, otherwise among all 96.
ctbot.promote then lets it trade on the main paper book inside a capped slice, and takes it off
again when its forward average turns negative (`keepers`).
"""

from __future__ import annotations

import json
import math
from datetime import date, time
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pandas as pd

from ctbot.library import signals as sg

REPORT = Path("data/research/index_rules.json")
FORWARD_FROM = "2026-10-28"  # the study's bars ended 2026-10-27
SEL, CONF = ("2000-01-01", "2012-12-31"), ("2013-01-01", "2026-10-27")
MIN_TRADES, MIN_DAYS, FDR_Q = 12, 60, 0.10
GATE_DAYS, STOP = 170, 0.08
SYMBOLS, HOLDS, GATES = ("SPY", "QQQ"), (1, 5), ("any", "above", "below")


def _drawdown(c: pd.Series) -> pd.Series:
    return c / c.rolling(20).max() - 1


# signal -> (plain words, rule on closes up to day t only)
SIGNALS: dict[str, tuple[str, Any]] = {
    "down3": ("fell 3% or more in a day", lambda c: c.pct_change() <= -0.03),
    "down4": ("fell 4% or more in a day", lambda c: c.pct_change() <= -0.04),
    "down5": ("fell 5% or more in a day", lambda c: c.pct_change() <= -0.05),
    "rsi5": ("2-day RSI under 5", lambda c: sg._rsi(c, 2) < 5),
    "rsi10": ("2-day RSI under 10", lambda c: sg._rsi(c, 2) < 10),
    "dd7": ("7% or more below its 20-day high", lambda c: _drawdown(c) <= -0.07),
    "dd10": ("10% or more below its 20-day high", lambda c: _drawdown(c) <= -0.10),
    "three_down": ("three down days in a row", lambda c: (c.diff() < 0) & (c.diff().shift(1) < 0) & (c.diff().shift(2) < 0)),
}
NAMES = [f"ix_{sym}_{s}_{g}_h{h}" for sym in SYMBOLS for s in SIGNALS for g in GATES for h in HOLDS]


def parts(name: str) -> tuple[str, str, str, int]:
    _, sym, rest = name.split("_", 2)
    s, g, h = rest.rsplit("_", 2)
    return sym, s, g, int(h[1:])


def title(name: str) -> str:
    sym, s, g, h = parts(name)
    where = {"any": "", "above": f" while QQQ is above its {GATE_DAYS}-day average", "below": f" while QQQ is below its {GATE_DAYS}-day average"}[g]
    return f"buy {sym} after it {'has ' if s in ('rsi5', 'rsi10') else ''}{SIGNALS[s][0]}{where}, hold {h} day{'s' if h > 1 else ''}"


def _gate(g: str, d: pd.DataFrame, qqq: pd.DataFrame) -> pd.Series:
    above = (qqq["close"] > qqq["close"].rolling(GATE_DAYS).mean()).reindex(d.index).fillna(False).astype(bool)
    return {"any": above | ~above, "above": above, "below": ~above}[g]


def fires(name: str, d: pd.DataFrame, qqq: pd.DataFrame) -> pd.Series:
    """True on day t when the rule says buy at the next open. Uses closes up to day t only."""
    _, s, g, _ = parts(name)
    return SIGNALS[s][1](d["close"]).fillna(False) & _gate(g, d, qqq)


def _trades(d: pd.DataFrame, sig: pd.Series, hold: int) -> pd.Series:
    """One return per trade, indexed by entry day: next open to the close `hold` days after the
    signal, never two trades at once (the study's rule)."""
    o, c, s, out, i = d["open"].to_numpy(), d["close"].to_numpy(), sig.to_numpy(), {}, 0
    while i < len(d) - hold:
        if s[i]:
            out[d.index[i + 1]] = c[i + hold] / o[i + 1] - 1
            i += hold + 1
        else:
            i += 1
    return pd.Series(list(out.values()), pd.DatetimeIndex(list(out)), dtype=float)


def _stat(t: pd.Series, base: float) -> dict[str, Any]:
    n = len(t)
    ex = t - (0.0 if math.isnan(base) else base)  # base is NaN when the period has no bars in the gate
    sd = float(ex.std()) if n > 2 else 0.0
    return {"n": n, "avg": round(float(t.mean()), 5) if n else None, "excess": round(float(ex.mean()), 5) if n else None,
            "t": round(float(ex.mean()) / (sd / math.sqrt(n)), 3) if sd > 0 else None}


def score(panel: dict[str, pd.DataFrame], today: date) -> dict[str, Any]:
    """All 96 rules on the bars in `panel` (SPY and QQQ daily)."""
    from ctbot.research.validation import benjamini_hochberg

    qqq, rows = panel["QQQ"], {}
    for name in NAMES:
        sym, _, g, hold = parts(name)
        d = panel[sym]
        sig = fires(name, d, qqq)
        t = _trades(d, sig, hold)
        every = (d["close"].shift(-hold) / d["open"].shift(-1) - 1)[_gate(g, d, qqq)]  # the same hold from any day in the gate
        row = {k: _stat(t[a:z], float(every[a:z].mean())) for k, (a, z) in (("sel", SEL), ("conf", CONF), ("forward", (FORWARD_FROM, None)))}
        s, c = row["sel"], row["conf"]
        row["history_ok"] = bool(s["n"] >= 10 and c["n"] >= 10 and s["t"] and c["t"] and s["excess"] > 0 and c["excess"] > 0
                                 and s["t"] >= 2 and c["t"] >= 1.5)
        row["fires_today"] = bool(len(sig) and sig.iloc[-1])
        rows[name] = row

    def p(r: dict[str, Any]) -> float:  # one-sided: how likely is a forward record this good from no edge
        f = r["forward"]
        return NormalDist().cdf(-f["t"]) if f["n"] >= MIN_TRADES and f["t"] is not None else 1.0

    prior = {n: p(r) for n, r in rows.items() if r["history_ok"]}
    keep = {n for fam in (prior, {n: p(r) for n, r in rows.items()}) if fam
            for n, ok in benjamini_hochberg(fam, FDR_Q).items() if ok}
    old = (today - date.fromisoformat(FORWARD_FROM)).days >= MIN_DAYS
    for n, r in rows.items():
        f = r["forward"]
        r["winning"] = bool(old and n in keep and f["n"] >= MIN_TRADES and f["avg"] > 0 and f["excess"] > 0)
    return {"day": today.isoformat(), "bars_through": str(qqq.index[-1].date()), "forward_from": FORWARD_FROM, "rules": rows}


def refresh(today: date, path: Path | None = None) -> dict[str, Any] | None:
    """Nightly: score from the bar cache and write the report; None when SPY or QQQ bars are missing."""
    from ctbot.research import factors as fx

    panel = fx.load_panel(SYMBOLS)
    if len(panel) < len(SYMBOLS):
        return None
    rep = score(panel, today)
    path = path or REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, indent=1))
    tmp.replace(path)
    return rep


def load(path: Path | None = None) -> dict[str, Any]:
    try:
        rules = json.loads((path or REPORT).read_text())["rules"]
        return {n: r for n, r in rules.items() if n in NAMES and isinstance(r, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def keepers(path: Path | None = None) -> set[str]:
    """Rules a promotion may keep trading: the forward record is still positive. A promoted rule
    whose record turns negative, or a report that cannot be read, takes it off the main book."""
    return {n for n, r in load(path).items()
            if (r.get("forward") or {}).get("n") and r["forward"]["avg"] > 0 and r["forward"]["excess"] > 0}


class IndexRule:
    """One promoted rule, live on the main paper book: decides once a day at the open on yesterday's
    completed bars, buys the index fund, and sells near the close `hold` days after the signal.
    No Claude review (a fixed rule).
    shortcut: the bot adds an 8% stop the backtest did not have, and skips the buy when the book
    already holds the fund (the QQQ core); replay with both if a promoted rule drifts from its record."""

    event_kinds: frozenset = frozenset()

    def __init__(self, name: str) -> None:
        self.name = name
        self.symbol, _, _, self.hold = parts(name)
        self._done = None

    def generate(self, ctx: Any) -> list:
        from ctbot.market_hours import ET, exit_by
        from ctbot.models import Direction, Signal
        from ctbot.strategies.rule import _frame
        from ctbot.strategies.swing import _add_bdays

        if ctx.now is None or self._done == ctx.today or not time(9, 35) <= ctx.now.astimezone(ET).time() <= time(10, 0):
            return []
        self._done = ctx.today
        if self.symbol in ctx.held_symbols:
            return []
        try:
            d, qqq = (_frame([b for b in ctx.market_data.daily_bars(s, 520) if b.ts.date() < ctx.today])
                      for s in (self.symbol, "QQQ"))
            if len(d) < 260 or len(qqq) < 260 or not bool(fires(self.name, d, qqq).iloc[-1]):
                return []
            q = ctx.market_data.quotes([self.symbol]).get(self.symbol)
        except Exception:  # noqa: BLE001 - no bars or no quote today: no trade
            return []
        if not q or not q.last:
            return []
        return [Signal(self.name, self.symbol, Direction.LONG, 0.6, float(self.hold), {
            "family": "index_rule", "rule": title(self.name), "source": "index rules study, 2026-11-18",
            "ref_price": round(q.last, 4), "stop": round(q.last * (1 - STOP), 4), "target": None,
            "exit_at": exit_by(_add_bdays(ctx.today, self.hold - 1)).isoformat(), "systematic": True,
            "size_pct": 1.0,  # one position at a time, so the whole of the rule's slice
            "decided_on_bars_through": str(d.index[-1].date())}, ts=ctx.now)]


def live_strategies(names: Any) -> list:
    from ctbot.strategies.registry import WeightedStrategy

    return [WeightedStrategy(IndexRule(n), 1.0, standalone=True, mode="trade") for n in sorted(names) if n in NAMES]
