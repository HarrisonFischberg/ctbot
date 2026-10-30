"""Score every trade idea after the fact: does Claude's review pick the better trades?

For each long idea with an exit plan (the intraday models), replay that plan on the cached
5-minute bars: enter at the open of the first bar starting at or after the idea, then exit at
the stop, the target, or the first bar at/after exit_at, whichever comes first (stop first if
a bar touches both). Costs as in the backtest. Approved and rejected ideas are scored the same
way, so the gap between them measures the value of the review.

`ctbot-score-ideas` writes data/idea_scores.json (read by the dashboard). Ideas whose exit is
still in the future, or whose bars are not cached yet, are left pending.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot.research import bars as cache
from ctbot.research.backtest import cost_bps


def _plan(idea: dict[str, Any]) -> dict[str, Any] | None:
    for s in idea.get("signals", []):
        if s.get("direction") == "long" and (s.get("evidence") or {}).get("exit_at"):
            return {**s["evidence"], "model": s["strategy"]}
    return None


def score_idea(idea: dict[str, Any], bars: list) -> dict[str, Any] | None:
    plan = _plan(idea)
    if not plan or not bars:
        return None
    t0 = datetime.fromisoformat(idea["ts"])
    exit_at = datetime.fromisoformat(plan["exit_at"])
    i = next((k for k, b in enumerate(bars) if b.ts >= t0), None)
    if i is None:
        return None
    if bars[-1].ts < exit_at and bars[-1].ts.date() < exit_at.astimezone(timezone.utc).date():
        return None  # exit day not cached yet
    entry = bars[i].open
    stop, target = plan.get("stop"), plan.get("target")
    for b in bars[i:]:
        if b.ts >= exit_at:
            px, why = b.open, "time"
            break
        if stop is not None and b.low <= stop:
            px, why = min(stop, b.open), "stop"
            break
        if target is not None and b.high >= target:
            px, why = max(target, b.open), "target"
            break
    else:
        return None
    c = cost_bps(idea["symbol"]) / 1e4
    d = idea.get("decision") or {}
    return {
        "id": idea["id"], "ts": idea["ts"], "symbol": idea["symbol"], "model": plan["model"],
        "approved": bool(d.get("approve")), "reviewed": bool(d), "confidence": d.get("confidence"),
        "status": idea.get("status"),
        "entry": entry, "exit": px, "exit_reason": why,
        "net_ret": px * (1 - c) / (entry * (1 + c)) - 1,
    }


def summarize(scores: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"approved": [], "rejected": []})
    for s in scores:
        # ideas saved without a review (shadow models) are kept apart so they don't dilute "rejected"
        key = "approved" if s["approved"] else "rejected" if s.get("reviewed", True) else "unreviewed"
        for g in (s["model"], "all"):
            groups[g].setdefault(key, []).append(s["net_ret"])

    def stat(r):
        return {"n": len(r), "avg_bps": statistics.mean(r) * 1e4 if r else None,
                "win_rate": sum(x > 0 for x in r) / len(r) if r else None}

    return {m: {k: stat(v) for k, v in g.items()} for m, g in groups.items()}


def calibration(scores: list[dict[str, Any]], width: float = 0.1) -> list[dict[str, Any]]:
    """Is Claude's confidence a real win probability? Reviewed ideas bucketed by confidence: the
    average stated confidence against the share that actually made money (the MAPIE review's
    reliability check; a bucket needs ~30 ideas before its win rate means much)."""
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s in scores:
        if s.get("confidence") is not None:
            buckets[min(int(s["confidence"] / width), round(1 / width) - 1)].append(s)
    return [{"from": round(k * width, 2), "to": round((k + 1) * width, 2), "n": len(b),
             "stated": statistics.mean(x["confidence"] for x in b),
             "win_rate": sum(x["net_ret"] > 0 for x in b) / len(b),
             "avg_bps": statistics.mean(x["net_ret"] for x in b) * 1e4} for k, b in sorted(buckets.items())]


def run(ideas_path: Path, out: Path, fetch=None) -> dict[str, Any]:
    """`fetch(symbol, start_date, end_date)` caches missing 5-minute bars (scanner symbols are
    outside the universe the scheduled job downloads); None skips fetching."""
    ideas = []
    if ideas_path.exists():
        for ln in ideas_path.read_text().splitlines():
            try:
                ideas.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    by_sym: dict[str, list] = {}
    scores, pending = [], 0
    for idea in ideas:
        if idea.get("direction") != "long" or not idea.get("decision"):
            continue  # only reviewed ideas: the comparison is approved vs rejected
        sym = idea["symbol"]
        if sym not in by_sym:
            by_sym[sym] = sorted(cache.load("5minute", sym), key=lambda b: b.ts)
        day = datetime.fromisoformat(idea["ts"]).date()
        if fetch and not any(b.ts.date() == day for b in by_sym[sym]):
            try:
                fetch(sym, day, day + timedelta(days=4))
                by_sym[sym] = sorted(cache.load("5minute", sym), key=lambda b: b.ts)
            except Exception:  # noqa: BLE001 - scored on a later run
                pass
        s = score_idea(idea, by_sym[sym])
        if s is None:
            pending += 1
        else:
            scores.append(s)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "scored": len(scores),
              "pending": pending, "summary": summarize(scores), "calibration": calibration(scores), "scores": scores[-500:]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    from ctbot.config import Settings

    p = argparse.ArgumentParser(description="Score reviewed trade ideas against what happened next")
    p.parse_args()
    s = Settings.from_env()
    fetch = None
    if s.market_data == "robinhood":
        from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient
        from ctbot.research.bars import fetch as fetch_bars

        client = ReadOnlyRobinhoodClient(interactive=False)
        fetch = lambda sym, a, b: fetch_bars([sym], "5minute", a, b, client.call_many)  # noqa: E731
    report = run(s.ideas_path, s.ideas_path.with_name("idea_scores.json"), fetch)
    print(f"scored {report['scored']} reviewed ideas ({report['pending']} pending)")
    for m, g in sorted(report["summary"].items()):
        a, r = g["approved"], g["rejected"]
        fmt = lambda x: "—" if x["avg_bps"] is None else f"{x['avg_bps']:+.1f} bps (n={x['n']}, win {x['win_rate'] * 100:.0f}%)"  # noqa: E731
        print(f"  {m:<12} approved {fmt(a):<34} rejected {fmt(r)}")
    for b in report["calibration"]:
        print(f"  confidence {b['from']:.1f}-{b['to']:.1f}: n={b['n']:<4} said {b['stated']:.0%}, "
              f"won {b['win_rate']:.0%}, {b['avg_bps']:+.1f} bps")


if __name__ == "__main__":
    main()
