"""Market context for Claude's reviews and the earnings guard. Read-only Robinhood tools.

Per trading day it caches the next 14 days of large-cap earnings reports and analyst
ratings; per call it adds the current ET time and how SPY/QQQ are doing today.

Optional public sources fill gaps: a TradingViewSnapshot adds next-report dates for names
outside Robinhood's large-cap calendar (the guard uses the earlier of the two) and technical
context; a MarketauxSentiment adds a numeric news-sentiment summary; SecEdgar adds recent 8-K
item codes and the last reported quarter's numbers (and its red-flag 8-Ks block entries). The
SPY regime and the daily chart setups come from Robinhood's own daily bars, and a ReplayOdds
replays an intraday model's plan on the stock's recent 5-minute bars. Every extra is advisory
and skipped when unavailable.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from ctbot import market_hours
from ctbot.analysis import regime, setups
from ctbot.market_hours import ET

log = logging.getLogger(__name__)

CallMany = Callable[[list[tuple[str, dict[str, Any]]]], list[dict[str, Any]]]
REPORT_TIME = {"am": time(8, 0), "pm": time(16, 5)}  # unknown timing -> assume before the open


def report_datetime(d: date, timing: str | None) -> datetime:
    return datetime.combine(d, REPORT_TIME.get(timing or "", time(8, 0)), tzinfo=ET)


class MarketContext:
    def __init__(self, call_many: CallMany, market_data=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 snapshot=None, news=None, sec=None, replay=None, catalysts=None) -> None:
        self._call_many = call_many
        self.md = market_data
        self.clock = clock
        self.snapshot = snapshot  # TradingViewSnapshot | None
        self.news = news  # MarketauxSentiment | None
        self.catalysts = catalysts  # AstralNews | None
        self.sec = sec  # SecEdgar | None
        self.replay = replay  # ReplayOdds | None
        self._day: date | None = None
        self._earnings: dict[str, tuple[date, str | None]] = {}
        self._ratings: dict[str, dict[str, Any]] = {}
        self._regime: dict[str, Any] | None = None

    def warm(self, symbols) -> None:
        """Register the universe so the TradingView snapshot fetches it in one request."""
        if self.snapshot is not None:
            self.snapshot.warm(symbols)

    # ---- daily caches ----------------------------------------------------------
    def _roll(self) -> None:
        today = market_hours.trading_date(self.clock())
        if self._day == today:
            return
        self._day, self._earnings, self._ratings, self._regime = today, {}, {}, None
        try:
            (cal,) = self._call_many([("get_earnings_calendar", {"days": 14, "filter": "high_market_cap"})])
            for e in (cal.get("data") or {}).get("results", []):
                rep = e.get("report") or {}
                if not e.get("symbol") or not rep.get("date"):
                    continue
                d = date.fromisoformat(rep["date"])
                sym = e["symbol"].upper()
                if d >= today and (sym not in self._earnings or d < self._earnings[sym][0]):
                    self._earnings[sym] = (d, rep.get("timing"))
        except Exception:  # noqa: BLE001 - context is advisory; never block the cycle
            log.exception("earnings calendar unavailable")

    def next_earnings(self, symbol: str) -> datetime | None:
        """The earlier of Robinhood's and TradingView's next report, so a gap in either source
        never lets a position ride through earnings."""
        self._roll()
        hit = self._earnings.get(symbol.upper())
        dates = [report_datetime(*hit)] if hit else []
        if self.snapshot is not None:
            try:
                tv = self.snapshot.next_earnings(symbol)
            except Exception:  # noqa: BLE001 - advisory source
                log.exception("TradingView earnings date unavailable")
                tv = None
            if tv is not None and market_hours.trading_date(tv) >= market_hours.trading_date(self.clock()):
                dates.append(tv.astimezone(ET))
        return min(dates) if dates else None

    def ratings(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self._roll()
        need = sorted({s.upper() for s in symbols} - set(self._ratings))
        if need:
            try:
                calls = [("get_equity_analyst_ratings", {"symbols": need[i:i + 75]}) for i in range(0, len(need), 75)]
                for payload in self._call_many(calls):
                    for r in (payload.get("data") or {}).get("results", []):
                        self._ratings[r["symbol"].upper()] = r.get("ratings") or {}
            except Exception:  # noqa: BLE001
                log.exception("analyst ratings unavailable")
            for s in need:
                self._ratings.setdefault(s, {})
        return {s.upper(): self._ratings.get(s.upper(), {}) for s in symbols}

    # ---- per review --------------------------------------------------------------
    def _day_change(self, sym: str) -> float | None:
        if self.md is None:
            return None
        try:
            q = self.md.quotes([sym]).get(sym)
            prev = self.md.daily_bars(sym, 1)
            return (q.last / prev[-1].close - 1) if q and prev and q.last else None
        except Exception:  # noqa: BLE001
            return None

    def market_regime(self) -> dict[str, Any] | None:
        """SPY's rule-based regime from completed daily bars; computed once per trading day."""
        self._roll()
        if self._regime is None and self.md is not None:
            try:
                self._regime = regime.classify([b.close for b in self.md.daily_bars("SPY", 300)]) or {}
            except Exception:  # noqa: BLE001
                log.exception("market regime unavailable")
                self._regime = {}
        return self._regime or None

    def red_flags(self, symbol: str) -> list[str]:
        """Red-flag 8-Ks (bankruptcy, delisting notice, non-reliance) of the last two weeks."""
        if self.sec is None:
            return []
        return self._extra("SEC 8-K red flags", lambda: self.sec.red_flags(symbol)) or []

    def plan_odds(self, symbol: str, plan: dict | None) -> dict[str, Any] | None:
        """Time-machine odds for a same-day exit plan (ctbot.research.timemachine)."""
        if self.replay is None or not plan:
            return None
        return self._extra("time machine", lambda: self.replay.for_plan(symbol, plan, self.clock()))

    def _extra(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - extras are advisory; never block a review
            log.exception("%s unavailable", name)
            return None

    def for_symbol(self, symbol: str) -> dict[str, Any]:
        now = self.clock()
        sym = symbol.upper()
        out: dict[str, Any] = {
            "now_et": now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET (%A)"),
            "minutes_to_close": round(market_hours.minutes_to_close(now)) if market_hours.session_open(now) else None,
            "spy_change_today": self._day_change("SPY"),
            "qqq_change_today": self._day_change("QQQ"),
            "symbol_change_today": self._day_change(sym),
        }
        out["market_regime"] = self.market_regime()
        if self.snapshot is not None:
            out["technicals"] = self._extra("TradingView technicals", lambda: self.snapshot.technicals(sym))
        if self.news is not None:
            out["news_sentiment"] = self._extra("news sentiment", lambda: self.news.for_symbol(sym))
        if self.catalysts is not None:
            out["news_catalysts"] = self._extra("Astral news", lambda: self.catalysts.for_symbol(sym))
        if self.md is not None:
            out["chart_setups"] = self._extra("chart setups", lambda: setups.for_symbol(self.md, sym))
        if self.sec is not None:
            out["sec_8k"] = self._extra("SEC 8-K items", lambda: self.sec.filings(sym))
            out["reported_numbers"] = self._extra("SEC reported numbers", lambda: self.sec.fundamentals(sym))
        nxt = self.next_earnings(sym)
        out["next_earnings"] = None if nxt is None else {
            "when_et": nxt.strftime("%Y-%m-%d %H:%M"), "days_away": (nxt.date() - now.astimezone(ET).date()).days}
        r = self.ratings([sym]).get(sym) or {}
        if r:
            mean = float(r["mean_price_target"]) if r.get("mean_price_target") else None
            last = None
            if self.md is not None:
                try:
                    q = self.md.quotes([sym]).get(sym)
                    last = q.last if q else None
                except Exception:  # noqa: BLE001
                    last = None
            out["analysts"] = {
                "buy": r.get("num_buy_ratings"), "hold": r.get("num_hold_ratings"), "sell": r.get("num_sell_ratings"),
                "mean_target": mean, "upside_to_mean_target": (mean / last - 1) if mean and last else None,
            }
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def holds_through_earnings(entry: datetime, exit_at: datetime, report: datetime | None) -> bool:
    return report is not None and entry < report < exit_at


def plan_exit(plan: dict | None, now: datetime) -> datetime:
    """Planned exit for a candidate; daily-horizon trades without a plan count as 30 days."""
    if plan and plan.get("exit_at"):
        return datetime.fromisoformat(plan["exit_at"])
    return now + timedelta(days=30)
