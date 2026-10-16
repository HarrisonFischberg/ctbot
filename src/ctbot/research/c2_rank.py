"""Rank Collective2 stock strategies and profile their closed trades. Read-only research.

`ctbot-c2-rank`:
  1. universe: strategies whose only enabled security type is stocks (CS)
  2. daily equity (net of commissions, not of subscription fees): 3/6/12-month returns,
     12-month max drawdown and Sharpe; drop series older than 10 days or with bad data
  3. for strategies positive in all three windows: details + closed trades, to measure what
     matters for this account: share of long trades, trades per year, holding period, cost
  4. writes data/c2/ranking_<date>.json and a watchlist (data/c2/watchlist.json) of the
     strategies that pass every filter; the copy_trading provider follows that watchlist.

All numbers are hypothetical model-account results from live strategies only (survivorship
bias), and a mechanical screen, not a recommendation.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

C2_DIR = Path("data/c2")
WATCHLIST = C2_DIR / "watchlist.json"
FILTERS = {
    "max_staleness_days": 10,
    "max_drawdown_12m": -0.25,
    "min_long_share": 0.95,
    "min_trades_12m": 12,
    "watchlist_size": 5,
}


def load_watchlist_ids(path: Path | None = None) -> list[int]:
    try:
        return [int(s["strategy_id"]) for s in json.loads((path or WATCHLIST).read_text())["strategies"]]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return []


def stocks_only(s: dict[str, Any]) -> bool:
    enabled = {k for k, v in (s.get("SecurityTypesEnabled") or {}).items() if v}
    return enabled == {"CS"}


def equity_stats(points: list[dict[str, Any]], today: date) -> dict[str, Any] | None:
    pts = sorted(
        ((datetime.fromisoformat(p["Date"].replace("Z", "+00:00")).date(), float(p["EquityWithCosts"]))
         for p in points if p.get("Date") and p.get("EquityWithCosts") is not None),
        key=lambda x: x[0],
    )
    if len(pts) < 30 or any(v <= 0 for _, v in pts):
        return None  # too short, or bad data (negative equity)
    end_d, end_v = pts[-1]
    if (today - end_d).days > FILTERS["max_staleness_days"]:
        return None

    def ret(days: int) -> float | None:
        start = [v for d, v in pts if d <= end_d - timedelta(days=days)]
        return end_v / start[-1] - 1 if start else None

    last12 = [(d, v) for d, v in pts if d >= end_d - timedelta(days=365)]
    peak, mdd = last12[0][1], 0.0
    for _, v in last12:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [b[1] / a[1] - 1 for a, b in zip(last12, last12[1:])]
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0
    return {
        "last_date": end_d.isoformat(), "r3m": ret(91), "r6m": ret(182), "r12m": ret(365),
        "maxdd_12m": mdd, "sharpe_12m": statistics.mean(rets) / sd * math.sqrt(252) if sd else None,
        "history_days": (end_d - pts[0][0]).days,
    }


def trade_profile(trades: list[dict[str, Any]], today: date) -> dict[str, Any]:
    def d(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    recent = [t for t in trades if t.get("CloseDate") and d(t["CloseDate"]).date() >= today - timedelta(days=365)]
    if not recent:
        return {"trades_12m": 0}
    longs = [t for t in recent if str(t.get("OpenSide")) == "1"]
    holds = [(d(t["CloseDate"]) - d(t["OpenDate"])).total_seconds() / 86400 for t in recent if t.get("OpenDate")]
    syms = set()
    for t in recent:
        s = t.get("ExchangeSymbol") or t.get("C2Symbol") or {}
        syms.add(s.get("Symbol") if isinstance(s, dict) else s)
    return {
        "trades_12m": len(recent),
        "long_share": len(longs) / len(recent),
        "median_hold_days": statistics.median(holds) if holds else None,
        "same_day_share": sum(h < 1 for h in holds) / len(holds) if holds else None,
        "win_rate_12m": sum(float(t.get("ProfitLoss") or 0) > 0 for t in recent) / len(recent),
        "symbols_12m": len(syms),
        "last_close": max(t["CloseDate"] for t in recent),
    }


def rank(client, today: date | None = None) -> dict[str, Any]:
    from ctbot.providers.collective2 import C2Forbidden

    today = today or datetime.now(timezone.utc).date()
    universe = [s for s in client.search_stocks() if s.get("IsAlive") and stocks_only(s)]
    log.info("stocks-only live strategies: %d", len(universe))
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(universe):
        sid = s["StrategyId"]
        try:
            st = equity_stats(client.daily_equity(sid), today)
        except C2Forbidden:
            st = None
        if st:
            rows.append({"strategy_id": sid, "name": s.get("StrategyName"), "monthly_cost": s.get("MonthlyCost"), **st})
        if i % 50 == 0:
            log.info("equity %d/%d", i, len(universe))
    for r in rows:
        r["positive_all_windows"] = all(r.get(k) is not None and r[k] > 0 for k in ("r3m", "r6m", "r12m"))
    candidates = [r for r in rows if r["positive_all_windows"]]
    log.info("fresh: %d, positive in all windows: %d", len(rows), len(candidates))
    for r in candidates:
        det = client.details(r["strategy_id"])
        r.update({
            "age_days": det.get("AgeInDays"), "suggested_capital": det.get("SuggestedCapital"),
            "trade_own_system": det.get("IsTradeOwnSystem"), "lifetime_return": det.get("Return"),
            "monthly_cost": det.get("MonthlyCost", r.get("monthly_cost")),
        })
        r.update(trade_profile(client.closed_trades(r["strategy_id"]), today))
        f = FILTERS
        r["fails"] = [name for name, ok in (
            ("drawdown", r["maxdd_12m"] >= f["max_drawdown_12m"]),
            ("shorts", r.get("long_share") is None or r["long_share"] >= f["min_long_share"]),
            ("few_trades", (r.get("trades_12m") or 0) >= f["min_trades_12m"]),
        ) if not ok]
    passing = sorted((r for r in candidates if not r["fails"]),
                     key=lambda r: r.get("sharpe_12m") or -9, reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "filters": FILTERS,
        "counts": {"stocks_only": len(universe), "fresh": len(rows), "positive_all_windows": len(candidates),
                   "pass_filters": len(passing)},
        "medians": {k: statistics.median([r[k] for r in rows if r.get(k) is not None])
                    for k in ("r3m", "r6m", "r12m") if any(r.get(k) is not None for r in rows)},
        "candidates": sorted(candidates, key=lambda r: r.get("sharpe_12m") or -9, reverse=True),
        "watchlist": passing[: FILTERS["watchlist_size"]],
    }


def main() -> None:
    import os

    from ctbot.providers.collective2 import C2Client
    from ctbot.secret_store import load_environment

    p = argparse.ArgumentParser(description="Rank Collective2 stock strategies (read-only)")
    p.add_argument("--no-watchlist", action="store_true", help="do not overwrite data/c2/watchlist.json")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request lines are noise here
    load_environment()
    report = rank(C2Client(os.environ.get("C2_API_KEY")))
    C2_DIR.mkdir(parents=True, exist_ok=True)
    out = C2_DIR / f"ranking_{datetime.now(timezone.utc):%Y%m%d}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    if not args.no_watchlist:
        WATCHLIST.write_text(json.dumps({"generated_at": report["generated_at"], "strategies": [
            {"strategy_id": r["strategy_id"], "name": r["name"], "monthly_cost": r.get("monthly_cost")}
            for r in report["watchlist"]]}, indent=2))
    print(json.dumps(report["counts"]), "| medians", {k: f"{v:+.1%}" for k, v in report["medians"].items()})
    hdr = f"{'id':>10} {'name':<28}{'3m':>7}{'6m':>7}{'12m':>7}{'DD':>7}{'Shrp':>6}{'$/mo':>6}{'tr/yr':>6}{'long':>6}{'hold d':>7}  fails"
    print(hdr)
    for r in report["candidates"][:25]:
        pct = lambda v: f"{v * 100:+.0f}%" if v is not None else "—"  # noqa: E731
        print(f"{r['strategy_id']:>10} {str(r['name'])[:27]:<28}{pct(r['r3m']):>7}{pct(r['r6m']):>7}{pct(r['r12m']):>7}"
              f"{pct(r['maxdd_12m']):>7}{(r.get('sharpe_12m') or 0):>6.2f}{(r.get('monthly_cost') or 0):>6.0f}"
              f"{r.get('trades_12m', 0):>6}{pct(r.get('long_share')):>6}{(r.get('median_hold_days') or 0):>7.1f}  {','.join(r['fails']) or 'PASS'}")
    print(f"\nwatchlist ({len(report['watchlist'])}): " + ", ".join(f"{r['name']} ({r['strategy_id']})" for r in report["watchlist"]))
    print(f"report: {out}")


if __name__ == "__main__":
    main()


# ---- forward tracking ("paper-follow" without a subscription) ----------------------

TRACK = C2_DIR / "track.json"


def track(client, today: date | None = None, spy_closes: dict[date, float] | None = None) -> dict[str, Any]:
    """Forward returns of watchlist strategies since they were added, vs SPY.

    Each strategy's start point is its daily equity on the day it entered the watchlist, so
    every number here is out-of-sample: none of it was visible when the strategy was picked.
    """
    today = today or datetime.now(timezone.utc).date()
    state = json.loads(TRACK.read_text()) if TRACK.exists() else {"strategies": {}}
    wl = json.loads(WATCHLIST.read_text()) if WATCHLIST.exists() else {"strategies": []}
    for s in wl["strategies"]:
        state["strategies"].setdefault(str(s["strategy_id"]), {"name": s["name"], "added": today.isoformat(),
                                                               "monthly_cost": s.get("monthly_cost")})
    rows = []
    for sid, info in state["strategies"].items():
        pts = sorted(
            ((datetime.fromisoformat(p["Date"].replace("Z", "+00:00")).date(), float(p["EquityWithCosts"]))
             for p in client.daily_equity(int(sid)) if p.get("EquityWithCosts") is not None),
            key=lambda x: x[0],
        )
        added = date.fromisoformat(info["added"])
        base = [(d, v) for d, v in pts if d <= added]
        if not base or not pts:
            continue
        base_d, base_v = base[-1]
        fwd = pts[-1][1] / base_v - 1
        spy = None
        if spy_closes:  # same window as the strategy: its base point to its latest point
            sb = [v for d, v in sorted(spy_closes.items()) if d <= base_d]
            se = [v for d, v in sorted(spy_closes.items()) if d <= pts[-1][0]]
            spy = se[-1] / sb[-1] - 1 if sb and se else None
        months = max((pts[-1][0] - added).days / 30.4, 0)
        rows.append({"strategy_id": int(sid), **info, "as_of": pts[-1][0].isoformat(), "forward_return": fwd,
                     "spy_return": spy, "months": months,
                     "fee_drag_on_250": (info.get("monthly_cost") or 0) * months / 250})
    state["rows"], state["updated"] = rows, datetime.now(timezone.utc).isoformat()
    TRACK.parent.mkdir(parents=True, exist_ok=True)
    TRACK.write_text(json.dumps(state, indent=2))
    return state


def track_main() -> None:
    import os

    from ctbot.providers.collective2 import C2Client
    from ctbot.secret_store import load_environment
    from ctbot.research import bars as bar_cache

    logging.basicConfig(level=logging.WARNING)
    load_environment()
    spy = {b.ts.date(): b.close for b in bar_cache.load("day", "SPY")}
    st = track(C2Client(os.environ.get("C2_API_KEY")), spy_closes=spy)
    print(f"{'id':>10} {'name':<28}{'added':>12}{'as of':>12}{'fwd':>8}{'SPY':>8}{'fee drag':>10}")
    for r in st["rows"]:
        spy_s = f"{r['spy_return'] * 100:+.1f}%" if r["spy_return"] is not None else "—"
        print(f"{r['strategy_id']:>10} {r['name'][:27]:<28}{r['added']:>12}{r['as_of']:>12}"
              f"{r['forward_return'] * 100:>+7.1f}%{spy_s:>8}{r['fee_drag_on_250'] * 100:>+9.1f}%")
    print("fee drag = subscription cost so far as a share of $250 (what following would have cost)")
