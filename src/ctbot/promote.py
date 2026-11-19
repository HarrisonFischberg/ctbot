"""Promotion: the step that moves a learned change onto the main paper book. Paper only.

`ctbot-learn promote` runs nightly after the journal. Code, not Claude, decides what qualifies:

* bench: a strategy the journal's scoreboard marks `losing` (30+ approved ideas, negative, significant
  after Benjamini-Hochberg) stops trading on main for BENCH_DAYS, then gets another chance, like
  the autopilot's own bench.
* lesson: a learner lesson whose changed verdicts made money over MIN_FLIPS of them (significant
  across all lessons tested, and at least LESSON_MIN_AGE_DAYS old) is shown to main's reviewer too.
* setting: a settings-test book that differs from main only in its Autopilot numbers and is "better"
  than main (variants._vs_main) over SETTING_MIN_DAYS gives main those numbers.

A qualifying change becomes a proposal in data/learning/promotions.json and nothing happens until
Harold approves it (dashboard, or `ctbot-learn approve <id>`). One proposal at a time, COOL_DAYS
between promotions so each effect can be read, and a declined or undone change is not proposed
again for BLOCK_DAYS. The base book never gets promotions: it is the control, and an active
promotion is undone when main falls behind base by more than luck (t below CHECK_T over
CHECK_MIN_DAYS+ days) or when the guard deletes its lesson. Every step is a line in the lessons'
hash-chained history. Only main reads this file: a variant book's own folder has none.

shortcut: factory rules are not promoted (main would need a capped sleeve per rule, and the factory
book has 1 closed trade), nor are test books that differ by an env setting; add them when a book
gets near the bar.
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
SETTING_KEYS = ("max_loss_pct", "giveback_pct", "min_confidence")


def _path(settings: Any) -> Path:
    return learn.learning_dir(settings) / "promotions.json"


def load(settings: Any) -> list[dict[str, Any]]:
    try:
        return json.loads(_path(settings).read_text()).get("items", [])
    except (OSError, ValueError):
        return []


def _save(settings: Any, items: list[dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2))
    tmp.replace(path)


def active(settings: Any) -> dict[str, Any]:
    """What main applies right now: {"bench": {strategy: reason}, "lessons": {lesson ids}}."""
    live = [x for x in load(settings) if x["status"] == "active"]
    return {"bench": {x["target"]: f"benched by the learning loop: {x['evidence']}" for x in live if x["kind"] == "bench"},
            "lessons": {x["target"] for x in live if x["kind"] == "lesson"}}


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
        e = effects.get(x["id"])
        old = now - datetime.fromisoformat(x["created"]) >= timedelta(days=LESSON_MIN_AGE_DAYS)
        if e and keep[x["id"]] and e["avg_bps"] > 0 and old:
            out.append({"kind": "lesson", "target": x["id"], "title": f'Show the main account\'s reviewer this lesson: "{x["text"]}"',
                        "evidence": f"in the learner book it changed {e['n']} verdicts and they came out "
                                    f"{e['avg_bps'] / 100:+.2f}% better each"})

    cfg = autopilot.load(settings.autopilot_path)
    try:
        books = {v.name: v for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name))}
        rows = variants.summary(settings)
    except (ValueError, OSError):
        books, rows = {}, []
    for r in rows:
        v, vs = books.get(r["name"]), r.get("vs_main") or {}
        if (v and v.strategies == "library" and not v.env and vs.get("verdict") == "better"
                and vs["days"] >= SETTING_MIN_DAYS and any(getattr(v.autopilot, k) != getattr(cfg, k) for k in SETTING_KEYS)):
            nums = ", ".join(f"{k.removesuffix('_pct').replace('_', ' ')} {getattr(v.autopilot, k):.0%}" for k in SETTING_KEYS)
            out.append({"kind": "setting", "target": v.name, "title": f"Give the main account the {v.name} test account's settings ({nums})",
                        "evidence": f"{v.name} beat main by {vs['edge']:+.3%} a day over {vs['days']} days"})
    return out


def _when(x: dict[str, Any], key: str) -> datetime:
    return datetime.fromisoformat(x[key])


def propose(settings: Any, journal: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """Add at most one proposal; None while one is waiting, inside the cooling-off, or nothing qualifies."""
    items = load(settings)
    if any(x["status"] == "proposed" for x in items):
        return None
    if any(x["status"] != "declined" and x["decided"] and now - _when(x, "decided") < timedelta(days=COOL_DAYS) for x in items):
        return None
    block = {(x["kind"], x["target"]) for x in items if x["status"] == "active"
             or (x["status"] in ("declined", "undone") and now - _when(x, "ended") < timedelta(days=BLOCK_DAYS))}
    lessons_full = len(active(settings)["lessons"]) >= MAX_PROMOTED_LESSONS
    for c in candidates(settings, journal, now):
        if (c["kind"], c["target"]) in block or (c["kind"] == "lesson" and lessons_full):
            continue
        item = {"id": uuid.uuid4().hex[:8], **c, "status": "proposed", "proposed": now.isoformat(timespec="seconds"),
                "decided": None, "ended": None, "why_ended": None, "undo": None}
        _save(settings, items + [item])
        _log(settings, "propose", item, now)
        _notify("CTbot: a change is waiting for your approval", f"{item['title']}\nWhy: {item['evidence']}.\n"
                "Approve or decline it on the dashboard's Autopilot page.")
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
        if item["kind"] == "setting":
            book = next(v for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name)) if v.name == item["target"])
            cfg = autopilot.load(settings.autopilot_path)
            new = {k: getattr(book.autopilot, k) for k in SETTING_KEYS}
            item["undo"] = {"before": {k: getattr(cfg, k) for k in SETTING_KEYS}, "after": new}
            autopilot.save(settings.autopilot_path, autopilot.validate({**asdict(cfg), **new}))
        item.update(status="active", decided=ts)
    _save(settings, items)
    _log(settings, "promote" if approve else "decline", item, now)
    return item


def _end(settings: Any, item: dict[str, Any], status: str, why: str, now: datetime) -> None:
    if item["kind"] == "setting" and item["undo"]:
        cfg = asdict(autopilot.load(settings.autopilot_path))
        if all(cfg[k] == v for k, v in item["undo"]["after"].items()):  # numbers changed by hand since are left alone
            autopilot.save(settings.autopilot_path, autopilot.validate({**cfg, **item["undo"]["before"]}))
    item.update(status=status, ended=now.isoformat(timespec="seconds"), why_ended=why)
    _log(settings, "demote", item, now, why)


def _main_vs_base(settings: Any, since: str) -> tuple[int, float]:
    """(days, t-stat) of main's daily return minus base's on the days after `since` (YYYY-MM-DD)."""
    data = settings.paper_ledger_path.parent
    main = variants._daily_returns(data / "equity_history.jsonl")
    base = variants._daily_returns(data / variants.ROOT_NAME / "base" / "equity_history.jsonl")
    d = [main[k] - base[k] for k in sorted(main.keys() & base.keys()) if k > since]
    return len(d), (stat(d)["t"] or 0.0)


def review(settings: Any, now: datetime) -> list[dict[str, Any]]:
    """End the active promotions that ran out or stopped paying; returns the ones ended."""
    items = load(settings)
    lessons = {x["id"] for x in learn.load_lessons(learn.learning_dir(settings))}
    ended = []
    for x in items:
        if x["status"] != "active":
            continue
        days, t = _main_vs_base(settings, x["decided"][:10])
        if x["kind"] == "bench" and now - _when(x, "decided") >= timedelta(days=BENCH_DAYS):
            _end(settings, x, "expired", f"{BENCH_DAYS} days are up: it trades again and must re-earn a bench", now)
        elif x["kind"] == "lesson" and x["target"] not in lessons:
            _end(settings, x, "undone", "the lesson was deleted", now)
        elif days >= CHECK_MIN_DAYS and t < CHECK_T:
            _end(settings, x, "undone", f"main fell behind the base account over the {days} days since (t {t:.1f})", now)
        else:
            continue
        ended.append(x)
    if ended:
        _save(settings, items)
        _notify("CTbot: a promoted change was ended", "\n".join(f"{x['title']}: {x['why_ended']}." for x in ended))
    return ended
