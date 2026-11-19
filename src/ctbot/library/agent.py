"""The research agent: once a night Claude proposes new entry rules and the strategy factory tests them.

`ctbot-learn research` makes one Claude call. It sees the factory's latest results on the selection
years (never the holdout), every rule the agent tried before and how it did, and the building blocks
below. It answers with at most MAX_NEW rules, each an AND of 1-4 blocks with numbers inside the
bounds. Claude never writes code: `check` validates every rule, and a rule is only data in
data/research/agent_rules.json. The next night's `ctbot-factory` run backtests each new rule like a
library rule (x filters x exits x universes) through the same gates.

What keeps a nightly idea machine honest:
* Every rule the agent ever tried stays counted. A rule that fails its first factory run is retired
  with its p-values, and the factory's Benjamini-Hochberg and Deflated Sharpe for agent rules run
  over all of them, so each failure raises the bar for the next idea. Library rules are judged as
  before, apart from the agent's.
* Claude has read about these years, and that hindsight leaks into its ideas and into the 2023+
  holdout alike. The test that cannot leak is forward: a passing rule trades in the factory paper
  book, and only its closed trades there can promote it to main (ctbot.promote).

Paper research only. Spend is capped by RESEARCH_MAX_USD_PER_DAY and logged as purpose "research".
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ctbot import llm_costs
from ctbot.library import signals as sg

STORE = Path("data/research/agent_rules.json")
MAX_NEW, MAX_CONDITIONS = 3, 4
RESEARCH_MODEL, RESEARCH_MAX_USD_PER_DAY = "claude-sonnet-5-5", 0.25
KEY_OK = re.compile(r"[a-z][a-z0-9_]{2,23}")
HOLD = (2, 120)


def _high(d: pd.DataFrame, n: int) -> pd.Series:
    return d["high"].rolling(n, min_periods=n).max().shift()


def _spy_ret(d: pd.DataFrame, spy: pd.DataFrame, n: int) -> pd.Series:
    sp = spy["close"].reindex(d.index)
    return sp / sp.shift(n)


def _down_days(d: pd.DataFrame, k: int) -> pd.Series:
    down = d["close"] < d["close"].shift()
    out = down
    for i in range(1, k):
        out = out & down.shift(i, fill_value=False)
    return out


# Building blocks: name -> (what it means, {parameter: (lowest, highest)}, rule on bars up to day t only).
# Every block looks back from day t (rolling windows and non-negative shifts), like the library rules.
BLOCKS: dict[str, tuple[str, dict[str, tuple[float, float]], Any]] = {
    "above_sma": ("close above its n-day average", {"n": (10, 250)},
                  lambda d, spy, n: d["close"] > sg._sma(d["close"], n)),
    "below_sma": ("close below its n-day average", {"n": (5, 250)},
                  lambda d, spy, n: d["close"] < sg._sma(d["close"], n)),
    "sma_cross_up": ("the fast average crossed above the slow one today", {"fast": (5, 100), "slow": (20, 250)},
                     lambda d, spy, fast, slow: (sg._sma(d["close"], fast) > sg._sma(d["close"], slow))
                     & (sg._sma(d["close"], fast).shift() <= sg._sma(d["close"], slow).shift())),
    "rsi_below": ("n-day RSI below x", {"n": (2, 21), "x": (2, 50)}, lambda d, spy, n, x: sg._rsi(d["close"], n) < x),
    "rsi_above": ("n-day RSI above x", {"n": (2, 21), "x": (50, 95)}, lambda d, spy, n, x: sg._rsi(d["close"], n) > x),
    "new_high": ("close above the highest high of the previous n days", {"n": (5, 252)},
                 lambda d, spy, n: d["close"] > _high(d, n)),
    "near_high": ("close within pct of the highest high of the previous n days", {"n": (20, 252), "pct": (0.01, 0.25)},
                  lambda d, spy, n, pct: d["close"] >= _high(d, n) * (1 - pct)),
    "drop": ("fell more than pct over n days", {"n": (1, 20), "pct": (0.01, 0.30)},
             lambda d, spy, n, pct: d["close"] / d["close"].shift(n) - 1 < -pct),
    "rise": ("rose more than pct over n days", {"n": (1, 126), "pct": (0.01, 0.60)},
             lambda d, spy, n, pct: d["close"] / d["close"].shift(n) - 1 > pct),
    "down_days": ("k lower closes in a row", {"k": (2, 6)}, lambda d, spy, k: _down_days(d, k)),
    "up_close": ("closed above the open", {}, lambda d, spy: d["close"] > d["open"]),
    "gap_down": ("opened more than pct below yesterday's low", {"pct": (0.002, 0.05)},
                 lambda d, spy, pct: d["open"] < d["low"].shift() * (1 - pct)),
    "volume_surge": ("volume above mult times its n-day average", {"n": (5, 60), "mult": (1.2, 5.0)},
                     lambda d, spy, n, mult: d["volume"] > mult * sg._sma(d["volume"], n).shift()),
    "narrow_range": ("today's range is the narrowest of n days", {"n": (4, 20)},
                     lambda d, spy, n: (d["high"] - d["low"]) <= (d["high"] - d["low"]).rolling(n, min_periods=n).min()),
    "calm": ("volatility in the quietest q share of its last year", {"q": (0.05, 0.5)},
             lambda d, spy, q: sg.ewma_vol(d) <= sg.ewma_vol(d).rolling(252, min_periods=252).quantile(q)),
    "shock_down": ("one-day drop bigger than mult times its usual daily swing", {"mult": (1.5, 4.0)},
                   lambda d, spy, mult: np.log(d["close"]).diff() < -mult * sg.ewma_vol(d).shift()),
    "beats_spy": ("beat the S&P 500 by more than pct over n days", {"n": (20, 252), "pct": (0.0, 0.30)},
                  lambda d, spy, n, pct: d["close"] / d["close"].shift(n) - _spy_ret(d, spy, n) > pct),
    "spy_above_sma": ("the S&P 500 is above its n-day average", {"n": (50, 250)},
                      lambda d, spy, n: (spy["close"] > sg._sma(spy["close"], n)).reindex(d.index).fillna(False)),
}
INT_PARAMS = {"n", "k", "fast", "slow"}


def load(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        rules = json.loads((path or STORE).read_text())["rules"]
        return [r for r in rules if isinstance(r, dict) and check(r, []) is None]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _save(rules: list[dict[str, Any]], path: Path | None = None) -> None:
    path = path or STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rules": rules}, indent=2))
    tmp.replace(path)


def _shape(rule: dict[str, Any]) -> str:
    """The rule's logic alone, so the same idea under a new name is caught."""
    return json.dumps(sorted((c["type"], sorted(c["params"].items())) for c in rule["conditions"]))


def check(rule: dict[str, Any], existing: list[dict[str, Any]]) -> str | None:
    """Why the rule is refused, or None. Code, not the model, decides what gets tested."""
    try:
        key, conds, hold = rule["key"], rule["conditions"], rule["max_hold"]
        if not isinstance(key, str) or not KEY_OK.fullmatch(key):
            return "key must be 3-24 characters: a-z, 0-9 and _"
        if not isinstance(hold, int) or isinstance(hold, bool) or not HOLD[0] <= hold <= HOLD[1]:
            return f"max_hold must be a whole number of days, {HOLD[0]}-{HOLD[1]}"
        if not isinstance(conds, list) or not 1 <= len(conds) <= MAX_CONDITIONS:
            return f"needs 1-{MAX_CONDITIONS} conditions"
        for c in conds:
            if c["type"] not in BLOCKS:
                return f"unknown building block {c['type']!r}"
            bounds = BLOCKS[c["type"]][1]
            if set(c["params"]) != set(bounds):
                return f"{c['type']} takes exactly: {', '.join(bounds) or 'no parameters'}"
            for p, (lo, hi) in bounds.items():
                v = c["params"][p]
                if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
                    return f"{c['type']}.{p} must be between {lo} and {hi}"
                if p in INT_PARAMS and v != int(v):
                    return f"{c['type']}.{p} must be a whole number"
        if any(c["type"] == "sma_cross_up" and c["params"]["fast"] >= c["params"]["slow"] for c in conds):
            return "sma_cross_up needs fast below slow"
        if any(r["key"] == key for r in existing):
            return f"the key {key} is already used"
        if any(_shape(r) == _shape(rule) for r in existing):
            return "the same conditions were already tried"
    except (KeyError, TypeError, AttributeError):
        return "malformed rule"
    return None


def family(rule: dict[str, Any]) -> sg.Family:
    conds = [(BLOCKS[c["type"]][2], {p: int(v) if p in INT_PARAMS else float(v) for p, v in c["params"].items()})
             for c in rule["conditions"]]

    def fn(d: pd.DataFrame, spy: pd.DataFrame) -> pd.Series:
        out = pd.Series(True, index=d.index)
        for f, params in conds:
            out = out & f(d, spy, **params).fillna(False).astype(bool)
        return out

    return sg.Family(f"ag_{rule['key']}", str(rule.get("title") or rule["key"])[:60], str(rule.get("tagline") or "")[:160],
                     rule["max_hold"], fn, f"research agent, {rule.get('added', '')[:10]}")


def families(path: Path | None = None, every: bool = False) -> dict[str, sg.Family]:
    """The agent's rules still under test or passing, as factory families (keys start `ag_`);
    `every` adds the retired ones, for a book still holding a rule's last positions."""
    return {f.key: f for f in (family(r) for r in load(path) if every or r.get("status") == "active")}


def retired(path: Path | None = None) -> dict[str, float]:
    """trial name -> p-value for every agent trial that failed, kept in the multiple-testing count for good."""
    return {k: float(v) for r in load(path) if r.get("status") == "retired" for k, v in (r.get("pvalues") or {}).items()}


def settle(report: dict[str, Any], path: Path | None = None) -> list[str]:
    """After a factory run: retire each agent rule that was tested and passed nowhere, keeping the
    p-values of its group winners. Returns the keys retired."""
    rules, out = load(path), []
    rows = report.get("candidates", {}).values()
    for r in rules:
        mine = [c for c in rows if c["family"] == f"ag_{r['key']}"]
        r["last"] = {c["name"]: {"passed": c["passed"], "pvalue": c["pvalue"], "dev_sharpe": c["dev"].get("sharpe"),
                                 "validation_sharpe": c["validation"].get("sharpe"), "trades": c["validation"].get("trades")}
                     for c in mine if c.get("pvalue") is not None} or r.get("last")
        if r.get("status") == "active" and mine and not any(c["passed"] for c in mine):
            r.update(status="retired", pvalues={c["name"]: c["pvalue"] for c in mine if c.get("pvalue") is not None})
            out.append(r["key"])
    if rules:
        _save(rules, path)
    return out


SYSTEM = """\
You are the research step of a paper-only equity trading bot. Each night you may propose up to 3 new
long-only entry rules for daily bars. The bot backtests every proposal tomorrow (2000-2022, split into
development and validation years, through overfitting controls) and paper-trades the ones that pass.
A rule is an AND of 1 to 4 building blocks from the list you are given, each with every one of its parameters given
as {name, value} inside its bounds, plus max_hold (trading days). True on day t means buy at the next open; exits (a stop at
2x the average range, a trailing stop, the longest hold) are shared and not yours to set.
You see how every library rule and every rule you proposed before performed. Every rule you ever
proposed stays in the multiple-testing count, so each failure raises the bar for all your later
ideas: propose a rule only when you have a reason it should work that is not "it fits the past", and
say that reason in `rationale` (the economic or behavioural effect it leans on). Prefer simple rules
with round, conventional numbers; do not re-try small variations of a rule that failed. Proposing
nothing is a good answer."""

RULES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["rules"],
    "properties": {"rules": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["key", "title", "tagline", "max_hold", "conditions", "rationale"],
        "properties": {"key": {"type": "string"}, "title": {"type": "string"}, "tagline": {"type": "string"},
                       "max_hold": {"type": "integer"}, "rationale": {"type": "string"},
                       "conditions": {"type": "array", "items": {
                           "type": "object", "additionalProperties": False, "required": ["type", "params"],
                           "properties": {"type": {"type": "string", "enum": sorted(BLOCKS)},
                                          "params": {"type": "array", "items": {
                                              "type": "object", "additionalProperties": False, "required": ["name", "value"],
                                              "properties": {"name": {"type": "string"}, "value": {"type": "number"}}}}}}}}}}},
}


def _brief(report: dict[str, Any] | None, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """What Claude sees: selection-years results only. The holdout never leaves the factory report."""
    winners = [{"rule": c["family"], "universe": c["universe"], "filter": c["filter"], "max_hold": c["max_hold"],
                "dev_sharpe": c["dev"].get("sharpe"), "validation_sharpe": c["validation"].get("sharpe"),
                "validation_trades": c["validation"].get("trades"), "pvalue": c["pvalue"], "passed": c["passed"]}
               for c in (report or {}).get("candidates", {}).values() if c.get("pvalue") is not None]
    return {"building_blocks": {k: {"means": v[0], "params": v[1]} for k, v in BLOCKS.items()},
            "library_rules": {f.key: f.tagline for f in sg.FAMILIES.values()},
            "group_winners": winners,
            "your_rules": [{k: r.get(k) for k in ("key", "tagline", "max_hold", "conditions", "rationale", "status", "last")}
                           for r in rules],
            "failed_trials_counted_against_you": sum(len(r.get("pvalues") or {}) for r in rules if r.get("status") == "retired")}


def propose(usage_path: Path, day: date, report: dict[str, Any] | None, client: Any = None,
            now: datetime | None = None, path: Path | None = None) -> list[dict[str, Any]] | None:
    """One Claude call; returns [{"key", "refused" (why, or None)}], or None when today's cap is spent."""
    model = os.environ.get("RESEARCH_MODEL", RESEARCH_MODEL)
    cap = float(os.environ.get("RESEARCH_MAX_USD_PER_DAY", RESEARCH_MAX_USD_PER_DAY))
    try:
        rows = [json.loads(ln) for ln in usage_path.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        rows = []
    if sum(r.get("usd", 0) for r in rows if r.get("purpose") == "research" and r.get("day") == day.isoformat()) >= cap:
        return None
    rules = load(path)
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    r = client.messages.create(model=model, max_tokens=4000, system=SYSTEM,
                               messages=[{"role": "user", "content": json.dumps(_brief(report, rules), default=str)}],
                               output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RULES_SCHEMA}})
    if getattr(r, "usage", None) is not None:
        llm_costs.record(usage_path, model, r.usage, "research", now)
    if r.stop_reason != "end_turn":
        raise ValueError(f"stop_reason {r.stop_reason}")
    text = next((b.text for b in r.content if b.type == "text"), "")
    out = []
    for rule in json.loads(text).get("rules", [])[:MAX_NEW]:
        try:  # the schema sends each block's parameters as [{name, value}]; rules are stored as {name: value}
            rule["conditions"] = [{"type": c["type"], "params": {p["name"]: p["value"] for p in c["params"]}}
                                  for c in rule["conditions"]]
        except (KeyError, TypeError):
            pass  # check() names what is wrong
        why = check(rule, rules)
        out.append({"key": rule.get("key"), "refused": why})
        if why is None:
            rules.append({**{k: rule[k] for k in ("key", "max_hold", "conditions")},
                          **{k: str(rule.get(k) or "")[:300] for k in ("title", "tagline", "rationale")},
                          "status": "active", "added": (now or datetime.now(UTC)).isoformat(timespec="seconds")})
    _save(rules, path)
    return out
