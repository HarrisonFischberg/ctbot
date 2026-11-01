"""Daily learning journal: stage 1 of the lessons loop (docs: the prime-agent assessment).

After the close, `ctbot-learn journal` writes data/learning/journal/YYYY-MM-DD.json. It records what
each paper book did that day and its running per-model record, the approved-vs-rejected idea
scores, the confidence calibration table and the factory's live list. Code only: no Claude call,
read-only on every book. A later stage reads the last few journals to propose reviewer lessons;
nothing reads them back into trading yet. The readout prints `changes()` as a "what changed" line.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ctbot import market_hours, readout, variants


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


def main(argv: list[str] | None = None) -> None:
    from ctbot.config import Settings

    p = argparse.ArgumentParser(description="CTbot learning loop (paper only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("journal", help="write the day's learning journal (code only, no Claude)")
    j.add_argument("day", nargs="?", type=date.fromisoformat, help="YYYY-MM-DD, default today's trading date")
    args = p.parse_args(argv)
    s = Settings.from_env()
    day = args.day or market_hours.trading_date(datetime.now(UTC))
    path = write(s, day)
    cur = json.loads(path.read_text())
    lines = changes(previous(path.parent, day), cur)
    print(f"written: {path}" + "".join(f"\n  {ln}" for ln in lines))


if __name__ == "__main__":
    main()
