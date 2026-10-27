"""Bub's thoughts: a few plain-language notes on where the market is going and how the bot is
choosing, written to data/bub_thoughts.jsonl and shown on the Home tab as "Bubs Thoughts".

Informational only. Nothing here changes what the bot buys or sells: every note is written from
values the cycle already has, and any failure is logged and ignored.

Two sources:
* `observe` runs once a cycle and looks for something worth saying: QQQ crossing its trend
  average, a big market day, the day's loss nearing the pull-back, a sector nearing its cap, a
  strategy's size shrinking or growing with its record, a strategy sitting out after losses, or
  one reason turning down most of today's ideas. Price alerts for what the bot holds (the
  day-trader pack's trader-alerts): a position near its stop, or up one times its risk (1R),
  each said once a day per stock.
* `before_buy` writes what the bot saw in an idea just before it submits the buy (or, in manual
  entry mode, before it asks for your click).

Not spammy: a note is skipped when the same thing was already said today (or, for a state such
as QQQ under its trend, until that state changes), and market notes are capped at
MAX_PER_HOUR an hour and MAX_PER_DAY a day. Buy notes aren't capped (each is one real buy) but
say each stock once a day. Text may contain "{models}", which the dashboard turns into strategy
names.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ctbot import market_hours

log = logging.getLogger(__name__)

MAX_PER_HOUR = 3
MAX_PER_DAY = 12
TAIL_LINES = 400  # dedupe and caps look this far back

BIG_DAY_PCT = 0.015       # QQQ up or down this much today is worth a note
NEAR_LIMIT_SHARE = 0.66   # the day's loss is this share of the pull-back trigger
NEAR_SECTOR_SHARE = 0.8   # a sector is this share of its cap
SMALL_SIZE = 0.6          # a strategy's next size at or under this (track-record sizing)
BIG_SIZE = 1.15           # ... or at or over this
REJECT_PATTERN_MIN = 5    # one reason turned down at least this many ideas today
NEAR_STOP_R = 0.25        # a held stock within this share of its risk above its stop

# risk reason code (or its prefix) -> what it means for choosing
REASON_WORDS = {
    "spread_too_wide": ("the gap between buy and sell prices was too wide",
                        "busy, heavily traded stocks have the best shot"),
    "illiquid": ("too few shares trade each day", "busy, heavily traded stocks have the best shot"),
    "max_open_positions": ("every position slot was full",
                           "new ideas only get in when something is sold"),
    "sector_cap": ("their sector was already at its cap", "ideas from other sectors have more room"),
    "pulled_back_no_borrowing": ("the bot is playing it safe and the cash is spent",
                                 "new buys wait for a sale or for the market to recover"),
    "earnings_before_exit": ("an earnings report fell before the planned sale",
                             "names without a report coming up are the ones that can trade"),
    "confidence": ("Claude wasn't sure enough", "only the clearest setups are getting through"),
    "trading_capital_cap": ("the most the bot may invest is already in use",
                            "new buys wait for something to be sold"),
}


def read(path: Path, n: int = 20) -> list[dict[str, Any]]:
    """The last `n` thoughts, newest first."""
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


def note(path: Path, now: datetime, kind: str, key: str, text: str, *, family: str | None = None,
         capped: bool = True, symbol: str | None = None, models: list[str] | None = None) -> dict | None:
    """Write one thought unless it repeats or the caps are reached. Returns it, or None.

    Without `family` a key is said once per trading day. With one, a note is skipped while the
    family's latest note has the same key (say "QQQ fell under its trend" once, until it changes).
    """
    recent = read(path, TAIL_LINES)
    today = market_hours.trading_date(now)

    def day(t: dict) -> Any:
        try:
            return market_hours.trading_date(datetime.fromisoformat(t["ts"]))
        except (KeyError, ValueError):
            return None

    if family is not None:
        last = next((t for t in recent if t.get("family") == family), None)
        if last is not None and last.get("key") == key:
            return None
    elif any(t.get("key") == key and day(t) == today for t in recent):
        return None
    if capped:
        mine = [t for t in recent if t.get("capped")]
        hour = sum(1 for t in mine if datetime.fromisoformat(t["ts"]) > now - timedelta(hours=1))
        if hour >= MAX_PER_HOUR or sum(1 for t in mine if day(t) == today) >= MAX_PER_DAY:
            return None
    rec: dict[str, Any] = {"ts": now.isoformat(), "kind": kind, "key": key, "text": text, "capped": capped}
    if family:
        rec["family"] = family
    if symbol:
        rec["symbol"] = symbol
    if models:
        rec["models"] = models
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        log.exception("thought write failed")
        return None
    log.info("bub thought: %s", text)
    return rec


def _last_family_key(path: Path, family: str) -> str | None:
    return next((t.get("key") for t in read(path, TAIL_LINES) if t.get("family") == family), None)


def _day_move(md, symbol: str, price: float | None) -> float | None:
    try:
        bars = md.daily_bars(symbol, 1)
    except Exception:  # noqa: BLE001
        return None
    if not bars or not price or not bars[-1].close:
        return None
    return price / bars[-1].close - 1


def _reason_family(code: str) -> str | None:
    if code.startswith("sector_cap"):
        return "sector_cap"
    if code.startswith("confidence"):
        return "confidence"
    return code if code in REASON_WORDS else None


def _today_rejections(ideas_path: Path, now: datetime) -> tuple[Counter, int]:
    """Today's turned-down ideas by reason family, and how many ideas came up today."""
    from ctbot.dashboard.state import _tail

    try:
        lines = _tail(ideas_path, 3000)
    except OSError:
        return Counter(), 0
    today = market_hours.trading_date(now)
    reasons: Counter = Counter()
    total = 0
    for ln in lines:
        try:
            idea = json.loads(ln)
            if market_hours.trading_date(datetime.fromisoformat(idea["ts"])) != today:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        total += 1
        if idea.get("status") != "rejected":
            continue
        fams = {f for f in (_reason_family(r) for r in ((idea.get("risk") or {}).get("reasons") or [])) if f}
        reasons.update(fams)
    return reasons, total


def observe(c, state, now: datetime) -> None:
    """Once a cycle: look for one or two things worth saying. Never raises."""
    try:
        _observe(c, state, now)
    except Exception:  # noqa: BLE001 - thoughts never block trading
        log.exception("bub thoughts failed")


def _observe(c, state, now: datetime) -> None:
    s, L = c.settings, c.settings.risk
    path = s.thoughts_path
    sym = L.risk_off_trend_symbol

    q = c.market_data.quotes([sym]).get(sym) if sym else None
    px = q.last if q else None
    avg = c.trend.avg if L.risk_off_trend_days > 0 else None
    if px and avg:
        gap = px / avg - 1
        if px < avg:
            note(path, now, "caution", "trend_below", family="trend", text=(
                f"{sym} is at ${px:,.2f}, {-gap:.1%} under its {L.risk_off_trend_days}-day average "
                f"(${avg:,.2f}). Dips under the trend line often keep sliding, so new buys use only cash "
                f"on hand until {sym} climbs back above it."))
        elif _last_family_key(path, "trend") == "trend_below":
            note(path, now, "market", "trend_above", family="trend", text=(
                f"{sym} is back above its {L.risk_off_trend_days}-day average (${px:,.2f} vs ${avg:,.2f}). "
                f"The uptrend is intact again, so buys may borrow again if nothing else holds them back."))

    move = _day_move(c.market_data, sym, px) if sym else None
    if move is not None and abs(move) >= BIG_DAY_PCT:
        if move > 0:
            note(path, now, "market", f"{sym}_up_big", text=(
                f"{sym} is up {move:.1%} today, a broad rally. Stocks that jumped with the market can give it "
                f"back fast, so the trailing stops matter more than usual today."))
        else:
            note(path, now, "caution", f"{sym}_down_big", text=(
                f"{sym} is down {-move:.1%} today, a broad sell-off. Swings this wide set off stop-losses more "
                f"often; the bot keeps its stops and doesn't buy more just because prices are lower."))

    if L.risk_off_day_loss_pct > 0 and state.day_start_equity > 0 and not state.risk_off:
        day = state.equity / state.day_start_equity - 1
        if -day >= NEAR_LIMIT_SHARE * L.risk_off_day_loss_pct:
            note(path, now, "caution", "day_loss_near", text=(
                f"The account is down {-day:.1%} today. At {L.risk_off_day_loss_pct:.0%} it starts playing it "
                f"safe: new buys use only cash on hand, no borrowing."))

    if L.max_sector_pct > 0 and state.equity > 0:
        for sector, value in sorted((state.sector_exposure or {}).items(), key=lambda kv: -kv[1]):
            share = value / state.equity
            if share >= NEAR_SECTOR_SHARE * L.max_sector_pct:
                note(path, now, "choice", f"sector_{sector}", text=(
                    f"{sector} is {share:.0%} of the account (the cap is {L.max_sector_pct:.0%}). New {sector} "
                    f"buys will shrink or get skipped; ideas from other sectors have more room."))
            break  # only the biggest

    for model, mult in sorted((c.risk.size_mult or {}).items()):
        if c.risk.model_paused(state, model):
            continue
        if mult <= SMALL_SIZE:
            note(path, now, "choice", f"size_small_{model}", models=[model], text=(
                f"{{models}} has been losing lately, so its next buys get {mult:.2f}x the usual money "
                f"until its record improves."))
        elif mult >= BIG_SIZE:
            note(path, now, "choice", f"size_big_{model}", models=[model], text=(
                f"{{models}} has been winning lately, so its next buys get {mult:.2f}x the usual money."))
    for model, streak in sorted((state.model_losing_streak or {}).items()):
        if c.risk.model_paused(state, model):
            note(path, now, "caution", f"paused_{model}", models=[model], text=(
                f"{{models}} lost {streak} trades in a row today, so it sits out the rest of the day."))

    _price_alerts(c, state, now)

    reasons, total = _today_rejections(s.ideas_path, now)
    if reasons:
        fam, n = reasons.most_common(1)[0]
        if n >= REJECT_PATTERN_MIN:
            why, hint = REASON_WORDS[fam]
            note(path, now, "choice", f"rejects_{fam}", text=(
                f"{n} of today's {total} ideas were turned down because {why}. {hint[0].upper()}{hint[1:]}."))


def _price_alerts(c, state, now: datetime) -> None:
    """Near the stop, or up 1R: a fact about a price, never an order (the brackets do the selling)."""
    positions = c.executor.positions()
    quotes = c.market_data.quotes([p.symbol for p in positions]) if positions else {}
    for p in positions:
        b, q = state.brackets.get(p.symbol) or {}, quotes.get(p.symbol)
        stop, entry = b.get("stop"), p.avg_cost
        if stop is None or not q or not q.bid or not entry:
            continue
        r0 = (b.get("ctx") or {}).get("r0") or (entry - stop if entry > stop else None)
        if not r0:
            continue
        if q.bid - stop <= NEAR_STOP_R * r0 and q.bid > stop:
            note(c.settings.thoughts_path, now, "caution", f"near_stop_{p.symbol}", symbol=p.symbol, text=(
                f"{p.symbol} is at ${q.bid:,.2f}, ${q.bid - stop:,.2f} above its stop (${stop:,.2f}). If it "
                f"slips under, the bot sells it; a fast drop can fill below the stop."))
        elif q.bid >= entry + r0:
            note(c.settings.thoughts_path, now, "market", f"up_1r_{p.symbol}", symbol=p.symbol, text=(
                f"{p.symbol} is up {(q.bid - entry) / r0:.1f}x what it risked (bought ${entry:,.2f}, now "
                f"${q.bid:,.2f}, stop ${stop:,.2f}). The plan decides when to sell, not the excitement."))


def before_buy(c, state, now: datetime, cand, quote, decision, verdict, waiting: bool = False) -> None:
    """What the bot saw in an idea, written just before it buys (or asks for your click)."""
    try:
        _before_buy(c, state, now, cand, quote, decision, verdict, waiting)
    except Exception:  # noqa: BLE001 - thoughts never block trading
        log.exception("bub buy thought failed for %s", getattr(cand, "symbol", "?"))


def _before_buy(c, state, now, cand, quote, decision, verdict, waiting) -> None:
    sym = cand.symbol
    notional = verdict.orders[0].notional if verdict and verdict.orders else None
    buyers = sorted({s.strategy for s in cand.signals if s.direction.value == "long"}) or list(cand.strategies)
    head = (f"Like {sym}" if waiting else f"Buying {sym}") + (f" (${notional:,.0f})" if notional else "")
    parts = [f"{head}: {{models}} {'agree' if len(buyers) > 1 else 'picked it'}"]
    conf = getattr(decision, "confidence", None)
    rationale = (getattr(decision, "rationale", None) or "").strip()
    if rationale and rationale != "systematic rule (no review)":
        snippet = rationale if len(rationale) <= 160 else rationale[:157].rsplit(" ", 1)[0] + "…"
        snippet = snippet.rstrip(".")
        parts.append(f"Claude is {conf:.0%} sure: “{snippet}”" if conf is not None else f"“{snippet}”")
    elif rationale:
        parts.append("a fixed rule, no Claude review")
    move = _day_move(c.market_data, sym, quote.last if quote else None)
    if move is not None and abs(move) >= 0.005:
        parts.append(f"{sym} is {'up' if move > 0 else 'down'} {abs(move):.1%} today")
    stop = getattr(decision, "stop_loss_pct", None)
    if stop:
        parts.append(f"it sells if it falls {stop:.0%}")
    if state.risk_off:
        parts.append("cash only while playing it safe")
    text = "; ".join(parts) + "." + (" Waiting for your click." if waiting else "")
    note(c.settings.thoughts_path, now, "idea" if waiting else "trade", f"buy_{sym}", text,
         capped=False, symbol=sym, models=buyers)
