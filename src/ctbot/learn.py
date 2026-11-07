"""Daily learning loop, paper only: the journal (stage 1) and reviewer lessons (stage 2).

After the close, `ctbot-learn journal` writes data/learning/journal/YYYY-MM-DD.json. It records what
each paper book did that day and its running per-model record, the approved-vs-rejected idea
scores, the confidence calibration table and the factory's live list. Code only: no Claude call,
read-only on every book. The readout prints `changes()` as a "what changed" line.

`ctbot-learn refine` (stage 2, after prime-agent's continual harness) makes one Claude call a night:
it reads the last 20 journals and the current lessons and proposes at most 3 create/update/delete
edits to data/learning/lessons.json. Code checks every edit before applying it (`check`): short
text, at most 12 lessons, models that exist, enough settled trades or scored ideas behind it in
the latest journal, and no wording about sizes, stops, loss caps, leverage or live trading. Each
applied edit is one hash-chained line in data/learning/history.jsonl; `ctbot-learn rollback <id>`
reverts one. Nothing reads the lessons into trading yet: that is stage 3, a separate paper book.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ctbot import llm_costs, market_hours, readout, tuning, variants
from ctbot.research.registry import GENESIS, _digest

log = logging.getLogger(__name__)


def _books(settings: Any) -> list[tuple[str, Any]]:
    from ctbot.config import Settings

    books = [("main", settings)]
    try:
        for v in variants.load(settings.strategies_file.with_name(variants.CONFIG.name)):
            books.append((v.name, Settings.from_env(variants.book_env({}, settings.paper_ledger_path, v))))
    except (ValueError, OSError):
        pass  # the readout reports a broken variants file; the journal keeps the main book
    return books


def _models(ledger: Path, day: date) -> dict[str, dict[str, Any]]:
    """Each model's settled record from the book's fills up to and including `day`."""
    from ctbot.dashboard.state import model_stats

    try:
        fills = json.loads(ledger.read_text()).get("fills", [])
        fills = [f for f in fills if market_hours.trading_date(datetime.fromisoformat(f["ts"])) <= day]
        stats = model_stats(fills, {})
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {s["model"]: {"closed": s["closed"], "win_rate": round(s["win_rate"], 3), "avg_ret": round(s["avg_ret"], 5),
                         "pnl": round(s["pnl"], 2)} for s in stats if s["closed"]}


def journal(settings: Any, day: date, factory_report: dict[str, Any] | None = None) -> dict[str, Any]:
    books = {}
    for name, s in _books(settings):
        start, end, _ = readout._equity(s.equity_history_path, day)
        _, closes = readout._trades(s.paper_ledger_path, day)
        books[name] = {"day_pnl": round(end - start, 2) if start is not None and end is not None else None,
                       "closed_today": len(closes), "wins_today": sum(c["pnl"] > 0 for c in closes),
                       "models": _models(s.paper_ledger_path, day)}
    try:
        scores = json.loads(settings.ideas_path.with_name("idea_scores.json").read_text())
        ideas = {k: scores.get(k) for k in ("generated_at", "scored", "summary", "calibration")}
    except (OSError, ValueError):
        ideas = None
    if factory_report is None:
        from ctbot.library import factory

        factory_report = factory.load_report() or {}
    fac = {"generated": factory_report.get("generated"), "n_candidates": factory_report.get("n_candidates"),
           "passed": len(factory_report.get("passed", [])), "live": factory_report.get("live", [])}
    return {"day": day.isoformat(), "written": datetime.now(UTC).isoformat(timespec="seconds"),
            "books": books, "ideas": ideas, "factory": fac}


def journal_dir(settings: Any) -> Path:
    return settings.paper_ledger_path.parent / "learning" / "journal"


def previous(folder: Path, day: date) -> dict[str, Any] | None:
    """The latest journal written for a day before `day`."""
    older = sorted(p for p in folder.glob("*.json") if p.stem < day.isoformat())
    try:
        return json.loads(older[-1].read_text()) if older else None
    except (OSError, ValueError):
        return None


def changes(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Plain-English differences from the previous journal: newly settled trades per main-book
    model (with the running win rate) and factory picks added or dropped."""
    out = []
    before = (prev or {}).get("books", {}).get("main", {}).get("models", {})
    for m, s in sorted(cur["books"].get("main", {}).get("models", {}).items()):
        new = s["closed"] - before.get(m, {}).get("closed", 0)
        if new > 0:
            out.append(f"{m}: {new} more closed, win rate now {s['win_rate']:.0%} over {s['closed']}")
    if prev:
        old, now = set(prev["factory"]["live"]), set(cur["factory"]["live"])
        out += [f"factory added {x}" for x in sorted(now - old)] + [f"factory dropped {x}" for x in sorted(old - now)]
    return out


def write(settings: Any, day: date) -> Path:
    folder = journal_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day.isoformat()}.json"
    path.write_text(json.dumps(journal(settings, day), indent=2, default=str))
    return path


# ---- stage 2: lessons ----------------------------------------------------------------------

MAX_LESSONS, MAX_EDITS, MAX_CHARS = 12, 3, 300
MIN_TRADES, MIN_IDEAS = 15, 30
JOURNALS_READ = 20
LEARN_MODEL = "claude-sonnet-5-5"
LEARN_MAX_USD_PER_DAY = 0.25
# Lessons advise the reviewer on judgment only; anything about money, size or risk limits stays with
# the risk manager, tuning.py's DENYLIST and Harold.
BANNED = re.compile(r"\b(live|real money|leverag\w*|margin|position siz\w*|notional|size up|stop[- ]loss\w*|"
                    r"loss (cap|limit)\w*|drawdown|kill switch|risk limit\w*|max_\w+|spend|budget)\b", re.IGNORECASE)

SYSTEM = """\
You maintain a short list of lessons for the trade-review stage of a paper-only equity bot. The
reviewer is Claude, judging one candidate trade at a time; lessons are added to its context.
You receive the bot's daily learning journals (oldest first; per-model settled paper trades, the
replay of approved vs rejected ideas per model, and the confidence calibration table) and the
current lessons. Propose at most 3 edits, or none. Each lesson is one or two sentences of review
judgment ("orb ideas approved in the last hour lost in 9 of 11 replays; demand a strong reason
late in the day"), tied to the models it concerns ("all" for every review), and cites the journal
numbers behind it. Only propose a lesson when the sample is large (15+ closed trades or 30+ scored
ideas for that model) and the pattern is clear; small samples are noise. Update or delete lessons
the newer data contradicts. Never write about position size, stops, loss limits, leverage, spend
or live trading: those are fixed rules outside your remit. Returning no edits is a good answer.
Keep each lesson's text under 300 characters, updates included: longer text is refused."""

EDITS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["edits"],
    "properties": {"edits": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["action", "id", "text", "models", "evidence"],
        "properties": {"action": {"type": "string", "enum": ["create", "update", "delete"]},
                       "id": {"type": ["string", "null"]}, "text": {"type": ["string", "null"]},
                       "models": {"type": "array", "items": {"type": "string"}},
                       "evidence": {"type": "string"}}}}},
}


def learning_dir(settings: Any) -> Path:
    return settings.paper_ledger_path.parent / "learning"


def load_lessons(folder: Path) -> list[dict[str, Any]]:
    try:
        return json.loads((folder / "lessons.json").read_text()).get("lessons", [])
    except (OSError, ValueError):
        return []


def history(folder: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(ln) for ln in (folder / "history.jsonl").read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def verify(folder: Path) -> bool:
    prev = GENESIS
    for r in history(folder):
        if r.get("prev_hash") != prev or _digest(r) != r.get("hash"):
            return False
        prev = r["hash"]
    return True


def _sample(model: str, latest: dict[str, Any]) -> tuple[int, int]:
    """(closed main-book trades, scored ideas) behind `model` in the latest journal."""
    if model == "all":
        trades = sum(m["closed"] for m in latest["books"].get("main", {}).get("models", {}).values())
    else:
        trades = latest["books"].get("main", {}).get("models", {}).get(model, {}).get("closed", 0)
    groups = ((latest.get("ideas") or {}).get("summary") or {}).get(model, {})
    return trades, sum((g or {}).get("n", 0) for g in groups.values())


def check(edit: dict[str, Any], lessons: list[dict[str, Any]], latest: dict[str, Any]) -> str | None:
    """Why the edit is refused, or None. Code, not the model, decides what is applied."""
    ids = {x["id"] for x in lessons}
    action = edit.get("action")
    if action not in ("create", "update", "delete"):
        return "unknown action"
    if action != "create" and edit.get("id") not in ids:
        return "no such lesson"
    if action == "delete":
        return None
    text, models = (edit.get("text") or "").strip(), edit.get("models") or []
    if not text or len(text) > MAX_CHARS:
        return f"text must be 1-{MAX_CHARS} characters"
    if action == "create" and len(lessons) >= MAX_LESSONS:
        return f"already {MAX_LESSONS} lessons"
    blob = text + " " + (edit.get("evidence") or "")
    if BANNED.search(blob) or any(k.lower() in blob.lower() for k in tuning.DENYLIST) or \
            any(p.lower() in blob.lower() for p in tuning.DENIED_PREFIXES):
        return "mentions sizes, stops, risk limits, spend or live trading"
    if not models:
        return "names no model"
    for m in models:
        trades, ideas = _sample(m, latest)
        if trades < MIN_TRADES and ideas < MIN_IDEAS:
            return f"{m}: {trades} closed trades and {ideas} scored ideas, below {MIN_TRADES}/{MIN_IDEAS}"
    return None


def _append(folder: Path, row: dict[str, Any]) -> dict[str, Any]:
    past = history(folder)
    row = {**row, "prev_hash": past[-1]["hash"] if past else GENESIS}
    row["hash"] = _digest(row)
    with (folder / "history.jsonl").open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def _save(folder: Path, lessons: list[dict[str, Any]]) -> None:
    tmp = folder / "lessons.json.tmp"
    tmp.write_text(json.dumps({"lessons": lessons}, indent=2))
    tmp.replace(folder / "lessons.json")


def apply(folder: Path, edits: list[dict[str, Any]], latest: dict[str, Any],
          now: datetime | None = None, trigger: str = "refine") -> list[dict[str, Any]]:
    """Check and apply up to MAX_EDITS edits; every edit, applied or refused, is logged."""
    folder.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    lessons, rows = load_lessons(folder), []
    for edit in edits[:MAX_EDITS]:
        why = check(edit, lessons, latest)
        before = next((x for x in lessons if x["id"] == edit.get("id")), None)
        after = None
        if why is None:
            lessons = [x for x in lessons if x["id"] != edit.get("id")]
            if edit["action"] != "delete":
                after = {"id": edit["id"] if edit["action"] == "update" else uuid.uuid4().hex[:8], "text": edit["text"].strip(),
                         "models": sorted(set(edit["models"])), "evidence": (edit.get("evidence") or "")[:500],
                         "created": (before or {}).get("created", ts), "updated": ts}
                lessons.append(after)
        rows.append(_append(folder, {"event": uuid.uuid4().hex[:8], "ts": ts, "trigger": trigger,
                                     "action": edit.get("action"), "before": before, "after": after,
                                     "evidence": (edit.get("evidence") or "")[:500], "refused": why}))
    _save(folder, lessons)
    return rows


def rollback(folder: Path, event: str, now: datetime | None = None) -> dict[str, Any]:
    """Undo one applied edit: restore the lesson as it was before that event."""
    row = next((r for r in history(folder) if r["event"] == event), None)
    if row is None or row["refused"]:
        raise ValueError(f"no applied edit {event}")
    target = (row["after"] or row["before"])["id"]
    lessons = [x for x in load_lessons(folder) if x["id"] != target] + ([row["before"]] if row["before"] else [])
    _save(folder, lessons)
    return _append(folder, {"event": uuid.uuid4().hex[:8], "ts": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                            "trigger": f"rollback {event}", "action": "rollback", "before": row["after"],
                            "after": row["before"], "evidence": "", "refused": None})


def _compact(j: dict[str, Any]) -> dict[str, Any]:
    ideas = j.get("ideas") or {}
    return {"day": j["day"], "main_models": j["books"].get("main", {}).get("models", {}),
            "book_day_pnl": {k: b.get("day_pnl") for k, b in j["books"].items()},
            "ideas": {k: ideas.get(k) for k in ("scored", "summary", "calibration")}, "factory_live": j["factory"]["live"]}


def refine(settings: Any, day: date, client: Any = None, now: datetime | None = None) -> list[dict[str, Any]] | None:
    """One Claude call proposing lesson edits; None when skipped (no journal, or today's cap spent)."""
    folder, usage = learning_dir(settings), settings.llm_usage_path
    paths = sorted(journal_dir(settings).glob("*.json"))
    paths = [p for p in paths if p.stem <= day.isoformat()][-JOURNALS_READ:]
    if not paths:
        return None
    model = os.environ.get("LEARN_MODEL", LEARN_MODEL)
    cap = float(os.environ.get("LEARN_MAX_USD_PER_DAY", LEARN_MAX_USD_PER_DAY))
    spent = sum(r.get("usd", 0) for r in _usage_rows(usage) if r.get("purpose") == "learn" and r.get("day") == day.isoformat())
    if spent >= cap:
        return None
    journals = [json.loads(p.read_text()) for p in paths]
    msg = json.dumps({"journals": [_compact(j) for j in journals], "lessons": load_lessons(folder)}, default=str)
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    r = client.messages.create(model=model, max_tokens=4000, system=SYSTEM,
                               messages=[{"role": "user", "content": msg}],
                               output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EDITS_SCHEMA}})
    if getattr(r, "usage", None) is not None:
        llm_costs.record(usage, model, r.usage, "learn", now)
    text = next((b.text for b in r.content if b.type == "text"), "")
    if r.stop_reason != "end_turn":
        raise ValueError(f"stop_reason {r.stop_reason}")
    return apply(folder, json.loads(text).get("edits", []), journals[-1], now)


def _usage_rows(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def main(argv: list[str] | None = None) -> None:
    from ctbot.config import Settings

    p = argparse.ArgumentParser(description="CTbot learning loop (paper only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("journal", help="write the day's learning journal (code only, no Claude)")
    j.add_argument("day", nargs="?", type=date.fromisoformat, help="YYYY-MM-DD, default today's trading date")
    r = sub.add_parser("refine", help="one Claude call proposing reviewer lessons (paper only)")
    r.add_argument("day", nargs="?", type=date.fromisoformat)
    sub.add_parser("lessons", help="print the current lessons")
    b = sub.add_parser("rollback", help="undo one applied lesson edit")
    b.add_argument("event")
    args = p.parse_args(argv)
    s = Settings.from_env()
    day = getattr(args, "day", None) or market_hours.trading_date(datetime.now(UTC))
    folder = learning_dir(s)
    if args.cmd == "refine":
        rows = refine(s, day)
        if rows is None:
            print("lessons: skipped (no journal yet, or today's learning budget is spent)")
        for row in rows or []:
            what = (row["after"] or row["before"] or {}).get("text", "")
            print(f"  {row['event']} {row['action']}: " + (f"refused ({row['refused']})" if row["refused"] else what))
        print(f"lessons: {len(load_lessons(folder))} active, history {'ok' if verify(folder) else 'BROKEN CHAIN'}")
        return
    if args.cmd == "lessons":
        for x in load_lessons(folder):
            print(f"{x['id']} [{', '.join(x['models'])}] {x['text']}")
        return
    if args.cmd == "rollback":
        print(f"rolled back: {rollback(folder, args.event)['after']}")
        return
    path = write(s, day)
    cur = json.loads(path.read_text())
    lines = changes(previous(path.parent, day), cur)
    print(f"written: {path}" + "".join(f"\n  {ln}" for ln in lines))


if __name__ == "__main__":
    main()
