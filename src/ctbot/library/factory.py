"""`ctbot-factory`: hundreds of strategy candidates a run, backtested and confirmed, paper research only.

Each candidate is one library entry rule (ctbot.library.signals) x one market filter x one exit
setting x one universe, so the grid is 12 x 4 x 9 x 2 = 864 candidates. Every candidate runs
through `library.backtest.simulate`, the same fills, stops and costs the autopilot uses. No Claude
calls and no new dependencies: the run costs CPU time only.

A candidate passes only if every gate holds, all on dev (to 2017) and validation (2018-22):
* the run's PBO across all candidates is under 0.5 (otherwise picking the best one is luck, and
  nothing passes);
* its validation p-value survives Benjamini-Hochberg at q=10% across all candidates;
* its validation Deflated Sharpe is at least 0.95, deflated for every candidate in the run;
* dev Sharpe above 0 and 20+ validation trades (the library's own admission rules);
* it still makes money in validation at 3x the normal costs.

Finalists then get one look at the holdout (2023 on), recorded once per candidate in the trial
registry. It is a sanity check after selection, never used to rank. The "live" list holds
finalists that also made money in the holdout, one per rule and universe. Only the `factory`
paper book (config/variants.toml) trades them, as systematic rules with their own give-back and
longest hold. Runs after the close from scripts/c2_track_weekly.sh. Writes data/research/factory_report.json.
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ctbot.library import backtest as lbt
from ctbot.library import signals as sg

REPORT = Path("data/research/factory_report.json")
GIVEBACKS = (0.05, 0.08, 0.12)
HOLD_MULTS = (0.5, 1.0, 2.0)
COST_STRESS = 3.0
EXPERIMENT = "strategy_factory"
MIN_DSR = 0.95
FDR_Q = 0.10


def _spy_trend(d, spy):
    c = spy["close"]
    return (c > c.rolling(200, min_periods=200).mean()).reindex(d.index).fillna(False)


def _volume(d, spy):
    return d["volume"] > d["volume"].rolling(20, min_periods=20).mean()


def _beats_spy(d, spy):
    sp = spy["close"].reindex(d.index)
    return (d["close"] / d["close"].shift(63)) > (sp / sp.shift(63))


# Market filters layered on an entry rule; like the rules, each uses bars up to day t only.
FILTERS = {"any": None, "spy_trend": _spy_trend, "volume": _volume, "beats_spy": _beats_spy}


def candidates() -> list[dict[str, Any]]:
    out = []
    for fam, filt, gb, hm, uni in itertools.product(sg.FAMILIES.values(), FILTERS, GIVEBACKS, HOLD_MULTS,
                                                    sg.UNIVERSES):
        hold = max(2, round(fam.max_hold * hm))
        out.append({"name": f"fx_{fam.key}_{filt}_g{round(gb * 100)}_h{hold}_{uni}", "family": fam.key,
                    "filter": filt, "giveback": gb, "max_hold": hold, "universe": uni})
    return out


def family_of(c: dict[str, Any]) -> sg.Family:
    base, f = sg.FAMILIES[c["family"]], FILTERS[c["filter"]]
    rule = base.rule if f is None else (lambda d, spy: base.rule(d, spy) & f(d, spy).astype(bool))
    return replace(base, key=c["name"], rule=rule, max_hold=c["max_hold"])


def _split(r: pd.Series, k: str) -> pd.Series:
    lo, hi = lbt.SPLITS[k]
    return r[(r.index >= lo) & (r.index <= hi)]


def run(panels: dict[str, dict[str, pd.DataFrame]], spy: pd.DataFrame,
        cands: list[dict[str, Any]] | None = None, progress: bool = False, registry=None) -> dict[str, Any]:
    from ctbot.research.lab import deflated_sharpe
    from ctbot.research.validation import benjamini_hochberg, pbo, sharpe_pvalue

    cands = [c for c in (cands or candidates()) if panels.get(c["universe"])]
    rows, daily, trades_of = {}, {}, {}
    t0 = time.monotonic()
    for i, c in enumerate(cands, 1):
        r, trades = lbt.simulate(panels[c["universe"]], spy, family_of(c), etf=(c["universe"] == "etfs"),
                                 giveback=c["giveback"])
        daily[c["name"]], trades_of[c["name"]] = r, trades
        rows[c["name"]] = {**c, **{k: lbt.metrics(r, trades, *lbt.SPLITS[k]) for k in ("dev", "validation")}}
        if progress and i % 50 == 0:
            print(f"  {i}/{len(cands)} candidates backtested ({time.monotonic() - t0:.0f}s)", flush=True)
    if not rows:
        raise SystemExit("no candidates ran: the price cache has none of the universe symbols")

    val = {n: _split(r, "validation").to_numpy() for n, r in daily.items()}
    pvals = {n: sharpe_pvalue(v) for n, v in val.items()}
    survivors = benjamini_hochberg(pvals, FDR_Q)
    sr = [v.mean() / v.std(ddof=1) for v in val.values() if len(v) > 2 and v.std(ddof=1) > 0]
    sr_var = float(np.var(sr, ddof=1)) if len(sr) > 1 else 0.0
    selection = pd.DataFrame({n: r[r.index <= lbt.SPLITS["validation"][1]] for n, r in daily.items()})
    run_pbo = pbo(selection.to_numpy()) if selection.shape[1] >= 2 and len(selection) >= 10 else None
    lucky = run_pbo is None or run_pbo["pbo"] >= 0.5

    passed = []
    for name, c in rows.items():
        dsr = deflated_sharpe(val[name].tolist(), len(rows), sr_var)["dsr"]
        checks = {"pbo": not lucky, "fdr": bool(survivors.get(name)), "dsr": dsr >= MIN_DSR,
                  "dev_sharpe_positive": c["dev"].get("sharpe", 0) > 0,
                  "enough_trades": c["validation"].get("trades", 0) >= 20}
        if all(checks.values()):  # costs last: it means another backtest, so only for finalists
            stressed, _ = lbt.simulate(panels[c["universe"]], spy, family_of(c), etf=(c["universe"] == "etfs"),
                                       giveback=c["giveback"], cost_mult=COST_STRESS)
            checks["survives_3x_costs"] = float((1 + _split(stressed, "validation")).prod()) > 1
        c.update(validation_pvalue=round(pvals[name], 5), dsr=round(dsr, 4), checks=checks,
                 passed=all(checks.values()))
        if c["passed"]:
            c["fires_today"] = _fires_today(c, panels[c["universe"]], spy)
            # a sanity check after selection, never a ranking input
            c["holdout"] = lbt.metrics(daily[name], trades_of[name], *lbt.SPLITS["holdout"])
            if registry is not None:
                _record_holdout(registry, c)
            passed.append(name)
    passed.sort(key=lambda n: -rows[n]["validation"].get("sharpe", 0))
    live, seen = [], set()
    for n in passed:  # one version per rule and universe: the variants of one rule move together
        key = (rows[n]["family"], rows[n]["universe"])
        if rows[n]["holdout"].get("cagr", -1) > 0 and key not in seen:
            seen.add(key)
            live.append(n)
    return {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "n_candidates": len(rows),
            "pbo": run_pbo, "splits": {k: lbt.SPLITS[k] for k in ("dev", "validation")},
            "gates": {"fdr_q": FDR_Q, "min_dsr": MIN_DSR, "cost_stress": COST_STRESS},
            "passed": passed, "live": live, "candidates": rows}


def _record_holdout(registry, c: dict[str, Any]) -> None:
    from ctbot.research.registry import HoldoutLocked

    params = {k: c[k] for k in ("family", "filter", "giveback", "max_hold", "universe")}
    try:
        registry.record(experiment=EXPERIMENT, strategy=c["name"], split="holdout", params=params,
                        periods=dict(zip(("from", "to"), lbt.SPLITS["holdout"])), metrics=c["holdout"],
                        data_fingerprint="factory")
    except HoldoutLocked:
        pass  # scored on an earlier night; the registry keeps the first result


def load_report(path: Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((path or REPORT).read_text())
    except (OSError, ValueError):
        return None


def live_strategies(report: dict[str, Any] | None) -> list:
    """The factory book's models: one systematic rule per candidate in report["live"], each with
    its own backtested give-back and longest hold (no Claude review)."""
    from ctbot.strategies.registry import WeightedStrategy
    from ctbot.strategies.rule import RuleStrategy

    out = []
    for name in (report or {}).get("live", []):
        c = report["candidates"][name]
        rs = RuleStrategy(c["family"], c["universe"], name, fam=family_of(c),
                          evidence={"systematic": True, "giveback": c["giveback"]})
        out.append(WeightedStrategy(rs, 1.0, standalone=True, mode="trade"))
    return out


def _fires_today(c: dict[str, Any], panel: dict[str, pd.DataFrame], spy: pd.DataFrame) -> list[str]:
    """Symbols whose rule is true on the latest cached bar (buy at the next open)."""
    fam = family_of(c)
    return sorted(s for s, df in panel.items() if len(df) and bool(fam.rule(df, spy).fillna(False).iloc[-1]))


MIN_HISTORY = 1000  # daily bars; fewer means only the nightly 30-day refresh ever reached this symbol


def backfill(symbols: list[str]) -> list[str]:
    """Fetch daily bars since 2000 for symbols whose cache is short; the dev split starts in 2000.
    A symbol listed later (META, ABBV...) has all of its history after one fetch and is skipped after."""
    from datetime import date

    from ctbot.research import bars

    short = sorted({s for s in symbols if len(bars.load("day", s)) < MIN_HISTORY})
    if short:
        from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient

        bars.fetch(short, "day", date(2000, 1, 1), date.today(), ReadOnlyRobinhoodClient(interactive=False).call_many)
        print(f"backfilled daily history for {len(short)} symbols: {', '.join(short)}", flush=True)
    return short


def main() -> None:
    from ctbot.research import factors as fx

    ap = argparse.ArgumentParser(description="Generate and backtest strategy candidates; keep the confirmed ones")
    ap.add_argument("--universe", choices=sorted(sg.UNIVERSES), help="only this universe (faster)")
    ap.add_argument("--limit", type=int, help="only the first N candidates (a quick smoke run)")
    ap.add_argument("--backfill", action="store_true", help="first fetch full daily history (since 2000) for "
                    "symbols with a short cache, from Robinhood (read-only)")
    args = ap.parse_args()
    unis = [args.universe] if args.universe else list(sg.UNIVERSES)
    if args.backfill:
        backfill(["SPY", *(s for u in unis for s in sg.universe(u))])
    spy = fx.load_panel(["SPY"]).get("SPY")
    if spy is None:
        raise SystemExit("no SPY daily bars in data/bars_cache/day: run ctbot-fetch-bars --interval day first")
    spy = spy[spy.index >= "1999-01-01"]
    panels = {u: fx.load_panel(sg.universe(u)) for u in unis}
    cands = [c for c in candidates() if c["universe"] in unis][: args.limit]
    print(f"backtesting {len(cands)} candidates ...", flush=True)
    from ctbot.research.registry import Registry

    rep = run(panels, spy, cands, progress=True, registry=Registry())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=2, default=str))
    p = rep["pbo"]
    print(f"PBO across the run: {p['pbo']:.2f}" if p else "PBO: not enough data")
    print(f"{'candidate':48} {'val CAGR':>9} {'Sh':>5} {'DSR':>5} {'trades':>6}  fires today")
    for name in rep["passed"][:25]:
        r = rep["candidates"][name]
        v = r["validation"]
        print(f"{name:48} {v.get('cagr', 0):>+9.1%} {v.get('sharpe', 0):>5.2f} {r['dsr']:>5.2f} "
              f"{v.get('trades', 0):>6}  {', '.join(r['fires_today']) or '-'}")
    print(f"passed {len(rep['passed'])} of {rep['n_candidates']} (PBO < 0.5, BH q=10%, DSR >= 0.95, "
          f"dev Sharpe > 0, 20+ trades, profitable at 3x costs)")
    print(f"factory book trades {len(rep['live'])}: {', '.join(rep['live']) or 'none'} "
          "(passed, profitable 2023-now, one per rule and universe)")
    print(f"written: {REPORT}")


if __name__ == "__main__":
    main()
