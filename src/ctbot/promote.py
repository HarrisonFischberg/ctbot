"""Promotion: the step that moves a learned change onto the main paper book. Paper only.

`ctbot-learn promote` runs nightly after the journal. Code, not Claude, decides what qualifies:

* bench: a strategy the journal's scoreboard marks `losing` (30+ approved ideas, negative, significant
  after Benjamini-Hochberg) stops trading on main for BENCH_DAYS, then gets another chance, like
  the autopilot's own bench.
* lesson: a learner lesson whose changed verdicts made money over MIN_FLIPS of them (significant
  across all lessons tested, and at least LESSON_MIN_AGE_DAYS old) is shown to main's reviewer too.
* setting: a settings-test book that differs from main only in its Autopilot numbers and is "better"
  than main (variants._vs_main) over SETTING_MIN_DAYS gives main those numbers.

* rule: a factory rule that is still on the factory's live list and made money over MIN_RULE_TRADES
  closed trades in the factory paper book (forward results, the only test Claude's hindsight and the
  backtest cannot leak into) trades on main too, in its own sleeve of RULE_PCT of the account, at
  most MAX_RULES of them.

A qualifying change becomes a proposal in data/learning/promotions.json. With PROMOTE_AUTO on (the
default since 2026-11-19, Harold's choice) the nightly run approves it itself; with PROMOTE_AUTO=false
nothing happens until Harold approves it (dashboard, or `ctbot-learn approve <id>`). Either way he
can undo an active one (dashboard, or `ctbot-learn undo <id>`). One proposal at a time, COOL_DAYS
between promotions so each effect can be read, and a declined or undone change is not proposed
again for BLOCK_DAYS. The base book never gets promotions: it is the control, and an active
promotion is undone when main falls behind base by more than luck (t below CHECK_T over
CHECK_MIN_DAYS+ days) or when the guard deletes its lesson. Every step is a line in the lessons'
hash-chained history. Only main reads this file: a variant book's own folder has none.

shortcut: test books that differ by an env setting are not promoted; add them when one gets near the bar.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

from ctbot import autopilot, learn, variants
from ctbot.research.score_ideas import stat

log = logging.getLogger(__name__)

MIN_FLIPS, LESSON_MIN_AGE_DAYS, MAX_PROMOTED_LESSONS = 30, 28, 3
SETTING_MIN_DAYS = 40
BENCH_DAYS, COOL_DAYS, BLOCK_DAYS = 30, 14, 30
CHECK_MIN_DAYS, CHECK_T = 10, -2.0
MIN_RULE_TRADES, RULE_PCT, MAX_RULES = 15, 0.10, 2
PROPOSAL_DAYS = 14  # an unanswered proposal lapses, so stale evidence is never approved and the queue moves
SETTING_KEYS = ("max_loss_pct", "giveback_pct", "min_confidence")
KEYS = {"id", "kind", "target", "title", "evidence", "status", "proposed", "decided", "ended", "why_ended", "undo"}
STATUSES = {"proposed", "active", "declined", "undone", "expired"}


def _path(settings: Any) -> Path:
    return learn.learning_dir(settings) / "promotions.json"


def load(settings: Any) -> list[dict[str, Any]]:
    """The promotion items; a missing, half-written or hand-broken file reads as none (main trades every cycle
    through this, so it must never raise)."""
    try:
        items = json.loads(_path(settings).read_text())["items"]
        return [x for x in items if isinstance(x, dict) and KEYS <= x.keys() and x["status"] in STATUSES]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _save(settings: Any, items: list[dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2))
    tmp.replace(path)


def auto() -> bool:
    return os.environ.get("PROMOTE_AUTO", "true").strip().lower() not in ("false", "0", "no", "off")


def active(settings: Any) -> dict[str, Any]:
    """What main applies right now: {"bench": {strategy: reason}, "lessons": {lesson id: the approved text},
    "rules": {factory rule names}}."""
    live = [x for x in load(settings) if x["status"] == "active"]
    return {"bench": {x["target"]: f"benched by the learning loop: {x['evidence']}" for x in live if x["kind"] == "bench"},
            "lessons": {x["target"]: x.get("text") for x in live if x["kind"] == "lesson"},
            "rules": frozenset(x["target"] for x in live if x["kind"] == "rule")}


def _log(settings: Any, action: str, item: dict[str, Any], now: datetime, why: str = "") -> None:
    learn._append(learn.learning_dir(settings), {
        "event": uuid.uuid4().hex[:8], "ts": now.isoformat(timespec="seconds"), "trigger": "promote", "action": action,
        "before": None, "after": {k: item[k] for k in ("id", "kind", "target", "status")},
        "evidence": (why or item["evidence"])[:500], "refused": None})


def _notify(title: str, body: str) -> None:
    url = os.environ.get("NOTIFY_URL", "").strip()
    if not url:
        return
    try:
        from ctbot import notify

        notify.send(url, title, body)
    except Exception:  # noqa: BLE001 - the phone notice is a courtesy; the proposal is already saved
        log.exception("promotion notice failed")


def candidates(settings: Any, journal: dict[str, Any], now: datetime) -> list[dict[str, str]]:
    """Every change that clears its bar tonight, most urgent first (cut losers before adding anything)."""
    from ctbot.research.validation import benjamini_hochberg

    out = [{"kind": "bench", "target": m, "title": f"Stop {m} trading on the main account for {BENCH_DAYS} days",
            "evidence": f"its {r['n']} approved ideas averaged {r['avg_bps'] / 100:+.2f}% each when replayed (p {r['p']})"}
           for m, r in (journal.get("scoreboard") or {}).items() if r["losing"]]

    effects = {k: e for k, e in (journal.get("lessons") or {}).items() if e["n"] >= MIN_FLIPS and e.get("t") is not None}
    keep = benjamini_hochberg({k: NormalDist().cdf(-e["t"]) for k, e in effects.items()}) if effects else {}
    for x in learn.load_lessons(learn.learning_dir(settings)):
        e = effects.get(x.get("id"))
        if e and keep[x["id"]] and e["avg_bps"] > 0 and _age(x.get("created"), now) >= timedelta(days=LESSON_MIN_AGE_DAYS):
            out.append({"kind": "lesson", "target": x["id"], "text": x["text"], "title": f'Show the main account\'s reviewer this lesson: "{x["text"]}"',
                        "evidence": f"it changed {e['n']} verdicts and they came out "
                                    f"{e['avg_bps'] / 100:+.2f}% better each"})

    cfg = autopilot.load(settings.autopilot_path)
    try:
        books = {v.name: v for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name))}
        rows = variants.summary(settings)
    except (ValueError, OSError):
        books, rows = {}, []
    for name, s in sorted(_factory_record(settings, books).items()):
        if s["closed"] >= MIN_RULE_TRADES and s["pnl"] > 0 and s["avg_ret"] > 0:
            out.append({"kind": "rule", "target": name,
                        "title": f"Let the factory rule {name} trade on the main account with up to {RULE_PCT:.0%} of it",
                        "evidence": f"in the factory test account its {s['closed']} closed trades averaged "
                                    f"{s['avg_ret']:+.2%} each and it still passes the nightly backtest"})
    from ctbot.library import index_rules

    held = {index_rules.parts(n)[0] for n in active(settings)["rules"] if n in index_rules.NAMES}
    for name, r in sorted(index_rules.winners(now.date()).items(), key=lambda kv: kv[1]["p"]):
        sym, _, gate, _ = index_rules.parts(name)
        # the QQQ core holds QQQ whenever it is above the gate, so such a rule could never buy; and
        # one index rule a fund: a second would be the same bet and the two would block each other
        if (sym, gate) != ("QQQ", "above") and sym not in held:
            held.add(sym)
            f = r["forward"]
            out.append({"kind": "rule", "target": name,
                        "title": f"Let this index rule trade on the main account with up to {RULE_PCT:.0%} of it: {index_rules.title(name)}",
                        "evidence": f"since {index_rules.FORWARD_FROM} its {f['n']} trades would have averaged {f['avg']:+.2%} each, "
                                    "which holds up after counting all 96 rules tried"})
    for r in rows:
        v, vs = books.get(r["name"]), r.get("vs_main") or {}
        if (v and v.strategies == "library" and not v.env and vs.get("verdict") == "better"
                and vs["days"] >= SETTING_MIN_DAYS and any(getattr(v.autopilot, k) != getattr(cfg, k) for k in SETTING_KEYS)):
            nums = ", ".join(f"{k.removesuffix('_pct').replace('_', ' ')} {getattr(v.autopilot, k):.0%}" for k in SETTING_KEYS)
            out.append({"kind": "setting", "target": v.name, "title": f"Give the main account the {v.name} test account's settings ({nums})",
                        "evidence": f"{v.name} beat main by {vs['edge']:+.3%} a day over {vs['days']} days"})
    return out


def _factory_record(settings: Any, books: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """rule -> its closed-trade record in the factory paper book(s), for rules on tonight's live list."""
    from ctbot.dashboard.state import model_stats
    from ctbot.library import factory

    live = set((factory.load_report() or {}).get("live", []))
    out: dict[str, dict[str, Any]] = {}
    for v in books.values():
        if v.strategies != "factory":
            continue
        try:
            ledger = settings.paper_ledger_path.parent / variants.ROOT_NAME / v.name / "paper_ledger.json"
            fills = json.loads(ledger.read_text()).get("fills", [])
        except (OSError, ValueError):
            continue
        out.update({s["model"]: s for s in model_stats(fills, {}) if s["model"] in live})
    return out


def apply_rules(c: Any, state: Any, runs: dict[str, float]) -> None:
    """Called by the pipeline each paper cycle: main trades its promoted factory and index rules as systematic
    strategies, each sized off its own sleeve (`runs`; the pipeline keeps the ones that are trading).
    A rule that leaves the list stops opening trades; what it holds keeps its stop and time exit."""
    from ctbot.library import factory, index_rules

    want = active(c.settings)["rules"]
    if want != c.promoted_rules:  # variant books never get here: no promotions file, so both are empty
        rep = factory.load_report() or {}
        live = {**rep, "live": sorted(n for n in want if n in rep.get("candidates", {}))}
        c.configured = ([ws for ws in c.configured if ws.strategy.name not in c.promoted_rules]
                        + factory.live_strategies(live) + index_rules.live_strategies(want))
        c.promoted_rules, c.launched = want, None
        log.info("promoted factory rules on this book: %s", ", ".join(sorted(want)) or "none")
    for name in want:
        runs.setdefault(name, RULE_PCT * state.equity)


def _age(ts: str | None, now: datetime) -> timedelta:
    """now - ts; zero for a missing or unreadable time, so nothing passes an age bar by accident."""
    try:
        t = datetime.fromisoformat(ts or "")
        return now - (t if t.tzinfo else t.replace(tzinfo=UTC))
    except ValueError:
        return timedelta(0)


def propose(settings: Any, journal: dict[str, Any], now: datetime, approve: bool = False) -> dict[str, Any] | None:
    """Add at most one proposal; None while one is waiting, inside the cooling-off, or nothing qualifies.
    `approve` (PROMOTE_AUTO) applies it at once."""
    items = load(settings)
    if any(x["status"] == "proposed" for x in items):
        return None
    if any(x["status"] != "declined" and x["decided"] and _age(x["decided"], now) < timedelta(days=COOL_DAYS) for x in items):
        return None
    # a lapsed proposal (never decided) may come straight back with fresh evidence; everything else waits
    block = {(x["kind"], x["target"]) for x in items if x["status"] == "active"
             or (x["decided"] and x["ended"] and _age(x["ended"], now) < timedelta(days=BLOCK_DAYS))}
    live = active(settings)
    full = {"lesson": len(live["lessons"]) >= MAX_PROMOTED_LESSONS, "rule": len(live["rules"]) >= MAX_RULES}
    for c in candidates(settings, journal, now):
        if (c["kind"], c["target"]) in block or full.get(c["kind"]):
            continue
        item = {"id": uuid.uuid4().hex[:8], **c, "status": "proposed", "proposed": now.isoformat(timespec="seconds"),
                "decided": None, "ended": None, "why_ended": None, "undo": None}
        _save(settings, items + [item])
        _log(settings, "propose", item, now)
        if approve:
            try:
                item = decide(settings, item["id"], True, now)
            except ValueError as e:  # cannot be applied any more: decline it so it does not block the queue
                log.warning("promotion %s not applied: %s", item["id"], e)
                decide(settings, item["id"], False, now)
                return None
        _notify("CTbot applied a learned change" if approve else "CTbot: a change is waiting for your approval",
                f"{item['title']}\nWhy: {item['evidence']}.\n"
                + ("You can undo it" if approve else "Approve or decline it") + " on the dashboard's Autopilot page.")
        return item
    return None


def decide(settings: Any, pid: str, approve: bool, now: datetime) -> dict[str, Any]:
    """Harold's answer to a proposal. Approving a setting writes main's autopilot.json and keeps the old numbers."""
    items = load(settings)
    item = next((x for x in items if x["id"] == pid and x["status"] == "proposed"), None)
    if item is None:
        raise ValueError(f"no waiting proposal {pid}")
    ts = now.isoformat(timespec="seconds")
    if not approve:
        item.update(status="declined", decided=ts, ended=ts, why_ended="declined")
    else:
        if item["kind"] == "lesson" and _lesson_text(settings, item["target"]) != item.get("text"):
            raise ValueError("that lesson was reworded or deleted since it was proposed: decline it")
        if item["kind"] == "setting":
            book = next((v for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name))
                         if v.name == item["target"]), None)
            if book is None:
                raise ValueError(f"the {item['target']} test account no longer exists: decline it")
            cfg = autopilot.load(settings.autopilot_path)
            new = {k: getattr(book.autopilot, k) for k in SETTING_KEYS}
            item["undo"] = {"before": {k: getattr(cfg, k) for k in SETTING_KEYS}, "after": new}
            autopilot.save(settings.autopilot_path, autopilot.validate({**asdict(cfg), **new}))
        item.update(status="active", decided=ts)
    _save(settings, items)
    _log(settings, "promote" if approve else "decline", item, now)
    return item


def _lesson_text(settings: Any, lid: str) -> str | None:
    return next((x.get("text") for x in learn.load_lessons(learn.learning_dir(settings)) if x.get("id") == lid), None)


def _end(settings: Any, item: dict[str, Any], status: str, why: str, now: datetime) -> None:
    if item["kind"] == "setting" and item["undo"]:
        cfg = asdict(autopilot.load(settings.autopilot_path))
        if all(cfg[k] == v for k, v in item["undo"]["after"].items()):  # numbers changed by hand since are left alone
            autopilot.save(settings.autopilot_path, autopilot.validate({**cfg, **item["undo"]["before"]}))
    item.update(status=status, ended=now.isoformat(timespec="seconds"), why_ended=why)
    _log(settings, "demote", item, now, why)


def undo(settings: Any, pid: str, now: datetime) -> dict[str, Any]:
    """Harold takes an active promotion back off main."""
    items = load(settings)
    item = next((x for x in items if x["id"] == pid and x["status"] == "active"), None)
    if item is None:
        raise ValueError(f"no active promotion {pid}")
    _end(settings, item, "undone", "undone by you", now)
    _save(settings, items)
    return item


def _main_vs_base(settings: Any, since: str) -> tuple[int, float]:
    """(days, t-stat) of main's daily return minus base's on the days after `since` (YYYY-MM-DD)."""
    data = settings.paper_ledger_path.parent
    main = variants._daily_returns(data / "equity_history.jsonl")
    base = variants._daily_returns(data / variants.ROOT_NAME / "base" / "equity_history.jsonl")
    d = [main[k] - base[k] for k in sorted(main.keys() & base.keys()) if k > since]
    return len(d), (stat(d)["t"] or 0.0)


def review(settings: Any, now: datetime) -> list[dict[str, Any]]:
    """End the active promotions that ran out or stopped paying, and the proposal nobody answered;
    returns the ones ended.
    shortcut: the main-vs-base test reruns nightly on overlapping data and ends every active promotion at once,
    so a harmless change is undone more often than t < -2 suggests (it errs toward undoing); give each
    promotion one fixed check date if good changes keep getting undone."""
    from ctbot.library import factory, index_rules

    items = load(settings)
    live_rules = set((factory.load_report() or {}).get("live", []))
    ended = []
    for x in items:
        if x["status"] == "proposed" and _age(x["proposed"], now) >= timedelta(days=PROPOSAL_DAYS):
            _end(settings, x, "expired", f"not answered in {PROPOSAL_DAYS} days", now)
            ended.append(x)
        if x["status"] != "active":
            continue
        days, t = _main_vs_base(settings, x["decided"][:10])
        if x["kind"] == "bench" and _age(x["decided"], now) >= timedelta(days=BENCH_DAYS):
            _end(settings, x, "expired", f"{BENCH_DAYS} days are up: it trades again and must re-earn a bench", now)
        elif x["kind"] == "lesson" and _lesson_text(settings, x["target"]) != x.get("text"):
            _end(settings, x, "undone", "the lesson was deleted or reworded", now)
        elif x["kind"] == "rule" and x["target"] in index_rules.NAMES and not index_rules.keep(x["target"], x["decided"][:10]):
            _end(settings, x, "undone", "the rule stopped winning in its nightly replay", now)
        elif x["kind"] == "rule" and x["target"] not in index_rules.NAMES and x["target"] not in live_rules:
            _end(settings, x, "undone", "the rule no longer passes the nightly backtest", now)
        elif days >= CHECK_MIN_DAYS and t < CHECK_T:
            _end(settings, x, "undone", f"main fell behind the base account over the {days} days since (t {t:.1f})", now)
        else:
            continue
        ended.append(x)
    if ended:
        _save(settings, items)
        _notify("CTbot: a promoted change was ended", "\n".join(f"{x['title']}: {x['why_ended']}." for x in ended))
    return ended
