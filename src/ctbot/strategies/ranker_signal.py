"""Shadow strategy: the walk-forward ranking model (`ctbot-rank-research --write-model`), live.

It reads the frozen model in data/research/ranker_model.json (feature list, ridge weights,
parameters, training dates), scores the universe on COMPLETED daily bars, and keeps the top k
with TopkDropout, exactly as the research simulator does. The research simulator decides on day
t's close and fills at day t+1's open, so this model decides once in the 09:35-10:00 ET window on
yesterday's completed bars and does not look at today's prices.

Systematic: no Claude review (no LLM cost); every hard risk check still applies. Ships in shadow
mode: it records ideas for the forward paper comparison and never trades unless you switch it to
trade on the dashboard. Without a model file it does nothing. A model file never changes its own
mode, and a newer model is only picked up after the file is replaced and the bot restarted.

TopkDropout runs on the model's own book (ranker_book.json next to the model file): the names it
picked at the last rebalance, persisted across restarts, not every position the account holds. A
rebalance emits EXIT for book names that dropped out (including names that lost their score) and
LONG only for new picks, so the forward record follows the strategy the research simulated. In
trade mode a pick the risk manager refused still counts as held until it drops out.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ctbot.market_hours import ET, next_business_day
from ctbot.models import Direction, EventKind, Signal
from ctbot.research import factors as fx
from ctbot.research.ranker import MODEL_FILE, topk_dropout
from ctbot.strategies.base import StrategyContext

log = logging.getLogger(__name__)
ANCHOR = date(2010, 1, 4)  # fixed origin for the rebalance calendar


def _add_bdays(d: date, n: int) -> date:
    for _ in range(n):
        d = next_business_day(d)
    return d


class RankerSignal:
    name = "ranker"
    event_kinds: frozenset[EventKind] = frozenset()

    def __init__(self, model_file: str | Path = MODEL_FILE, disaster_stop: float = 0.15,
                 max_model_age_days: int = 120, book_file: str | Path | None = None) -> None:
        self.model_file = Path(model_file)
        self.book_file = Path(book_file) if book_file else self.model_file.with_name("ranker_book.json")
        self.disaster_stop = disaster_stop
        self.max_model_age_days = max_model_age_days
        self._model: dict | None = None
        self._loaded = False
        self._done: date | None = None

    def model(self) -> dict | None:
        if not self._loaded:
            self._loaded = True
            try:
                self._model = json.loads(self.model_file.read_text())
            except FileNotFoundError:
                log.info("ranker: no model file at %s (run ctbot-rank-research --write-model)", self.model_file)
            except (OSError, ValueError):
                log.exception("ranker: unreadable model file %s", self.model_file)
        return self._model

    def book(self) -> list[str]:
        try:
            return list(json.loads(self.book_file.read_text()).get("held", []))
        except (OSError, ValueError):
            return []

    def _save_book(self, held: list[str], day: date, version: str) -> None:
        self.book_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.book_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"as_of": day.isoformat(), "model": version, "held": sorted(held)}))
        tmp.replace(self.book_file)

    def rebalance_day(self, d: date, every: int) -> bool:
        return int(np.busday_count(ANCHOR, d)) % max(every, 1) == 0

    def scores(self, ctx: StrategyContext, m: dict) -> pd.Series:
        p = m["params"]
        panel = {}
        for sym in m["universe"]:
            if sym not in ctx.universe and sym != "SPY":
                continue
            try:
                bars = [b for b in ctx.market_data.daily_bars(sym, 300) if b.ts.date() < ctx.today]
            except Exception:  # noqa: BLE001
                log.warning("ranker: no daily bars for %s", sym)
                continue
            if len(bars) >= 260:
                panel[sym] = pd.DataFrame(
                    {f: [getattr(b, f) for b in bars] for f in fx.FIELDS},
                    index=pd.DatetimeIndex([pd.Timestamp(b.ts.date()) for b in bars]))
        if len(panel) < max(5, p["top_k"] + 2):
            return pd.Series(dtype=float)
        feats = fx.compute(panel)
        last = max(df.index[-1] for df in panel.values())
        rows = {k: fx.cross_sectional_rank(feats[k]).loc[last] for k in m["features"]}
        x = pd.DataFrame(rows)[m["features"]].fillna(0.0)
        s = pd.Series(x.to_numpy() @ np.array(m["weights"]), index=x.index)
        close = fx.wide(panel, "close").loc[last]
        adv = (fx.wide(panel, "close") * fx.wide(panel, "volume")).rolling(20).mean().loc[last]
        ok = (close >= p["min_price"]) & (adv >= p["min_adv"]) & (s.index != "SPY")
        return s[ok].dropna()

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        m = self.model()
        if m is None or ctx.now is None or self._done == ctx.today:
            return []
        t = ctx.now.astimezone(ET).time()
        if not time(9, 35) <= t <= time(10, 0):
            return []
        self._done = ctx.today  # once per day, whatever happens below
        p = m["params"]
        if not self.rebalance_day(ctx.today, p["rebalance_every"]):
            return []
        trained = date.fromisoformat(m["trained_through"])
        if (ctx.today - trained).days > self.max_model_age_days:
            log.warning("ranker: model trained through %s is older than %d days; no signals",
                        trained, self.max_model_age_days)
            return []
        s = self.scores(ctx, m)
        if s.empty:
            return []
        names = list(s.index)
        book = self.book()
        held = {names.index(h) for h in book if h in names}
        keep = topk_dropout(s.to_numpy(), held, p["top_k"], p["n_drop"])
        self._save_book([names[j] for j in keep], ctx.today, m["version"])
        ev = {"systematic": True, "model": m["version"], "trained_through": m["trained_through"],
              "decided_on_bars_through": str(ctx.today - timedelta(days=1)), "horizon_days": p["horizon"]}
        out: list[Signal] = []
        for j in sorted(held - keep):
            out.append(Signal(self.name, names[j], Direction.EXIT, 1.0, 0,
                              {**ev, "reason": "dropped_from_top_k", "score": round(float(s.iloc[j]), 5)}, ts=ctx.now))
        for sym in sorted(set(book) - set(names)):  # lost its score (no data, illiquid): research sells it
            out.append(Signal(self.name, sym, Direction.EXIT, 1.0, 0, {**ev, "reason": "no_score"}, ts=ctx.now))
        # Held names leave at a rebalance (EXIT above); exit_at is only a safety net.
        exit_day = _add_bdays(ctx.today, 60)
        buys = [names[j] for j in sorted(keep - held)]
        quotes = ctx.market_data.quotes(buys) if buys else {}
        for sym in buys:
            q = quotes.get(sym)
            if not q or not q.last:
                continue  # no live price: no entry (the risk manager would refuse it anyway)
            out.append(Signal(self.name, sym, Direction.LONG, 0.6, float(p["horizon"]), {
                **ev, "score": round(float(s[sym]), 5), "size_pct": round(1.0 / p["top_k"], 4),
                "ref_price": round(q.last, 4), "stop": round(q.last * (1 - self.disaster_stop), 4),
                "target": None, "exit_at": datetime.combine(exit_day, time(15, 50), tzinfo=ET).isoformat(),
            }, ts=ctx.now))
        return out
