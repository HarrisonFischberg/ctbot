"""Research agent: Claude proposes entry rules as data; code checks them, the factory tests them, failures stay counted."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest
from test_factory import _panel

from ctbot.library import agent
from ctbot.library import factory as fy
from ctbot.library import signals as sg

NOW = datetime(2026, 11, 19, 22, tzinfo=UTC)


def _rule(key="dip_in_trend", conds=None, hold=5, **kw):
    conds = conds or [{"type": "rsi_below", "params": {"n": 3, "x": 15}}, {"type": "above_sma", "params": {"n": 200}}]
    return {"key": key, "title": "Dip", "tagline": "buys dips", "max_hold": hold, "conditions": conds, "rationale": "reversal", **kw}


def test_check_accepts_only_known_blocks_inside_their_bounds():
    assert agent.check(_rule(), []) is None
    bad = lambda **kw: agent.check(_rule(**kw), [])  # noqa: E731
    assert "unknown building block" in bad(conds=[{"type": "import_os", "params": {}}])
    assert "between" in bad(conds=[{"type": "rsi_below", "params": {"n": 3, "x": 99}}])
    assert "whole number" in bad(conds=[{"type": "above_sma", "params": {"n": 50.5}}])
    assert "takes exactly" in bad(conds=[{"type": "above_sma", "params": {"n": 50, "shift": -5}}])  # no way to peek ahead
    assert "between" in bad(conds=[{"type": "above_sma", "params": {"n": True}}])
    assert "1-4 conditions" in bad(conds=[{"type": "up_close", "params": {}}] * 5)
    assert "fast below slow" in bad(conds=[{"type": "sma_cross_up", "params": {"fast": 100, "slow": 50}}])
    assert "max_hold" in bad(hold=500) and "key" in bad(key="../x")
    assert agent.check({"key": "abc"}, []) == "malformed rule"
    assert "already used" in agent.check(_rule(), [_rule()])
    assert "already tried" in agent.check(_rule(key="other_name"), [_rule()])  # the same idea renamed


MID = {k: {p: (round((lo + hi) / 2) if p in agent.INT_PARAMS else (lo + hi) / 2) for p, (lo, hi) in v[1].items()}
       for k, v in agent.BLOCKS.items()}
MID["sma_cross_up"] = {"fast": 20, "slow": 50}


@pytest.mark.parametrize("block", sorted(agent.BLOCKS))
def test_every_building_block_uses_only_the_past(block):
    p = _panel(700, ("AAA", "SPY"))
    rule = agent.family(_rule(conds=[{"type": block, "params": MID[block]}])).rule
    full = rule(p["AAA"], p["SPY"])
    for cut in (400, 699):
        part = rule(p["AAA"].iloc[:cut], p["SPY"].iloc[:cut])
        assert (part == full.iloc[:cut]).all()


class _Client:
    def __init__(self, rules):
        self.calls, self.rules, self.messages = 0, rules, self

    def create(self, **kw):
        self.calls += 1
        self.seen = json.loads(kw["messages"][0]["content"])
        wire = [{**r, "conditions": [{"type": c["type"], "params": [{"name": k, "value": v} for k, v in c["params"].items()]}
                                     for c in r["conditions"]]} for r in self.rules]
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"rules": wire}))],
                  usage={"input_tokens": 100_000, "output_tokens": 0})


def test_propose_stores_checked_rules_hides_the_holdout_and_stops_at_the_cap(tmp_path):
    store, usage = tmp_path / "agent_rules.json", tmp_path / "llm_usage.jsonl"
    report = {"candidates": {"fx_rsi2_any_g8_h5_etfs": {
        "name": "fx_rsi2_any_g8_h5_etfs", "family": "rsi2", "universe": "etfs", "filter": "any", "max_hold": 5,
        "dev": {"sharpe": 0.4}, "validation": {"sharpe": 0.9, "trades": 80}, "pvalue": 0.01, "passed": True,
        "holdout": {"sharpe": 9.99, "cagr": 0.5}}}}
    c = _Client([_rule(), _rule(key="peek", conds=[{"type": "above_sma", "params": {"n": 9999}}]),
                 _rule(key="same_again"), _rule(key="fourth", conds=[{"type": "up_close", "params": {}}])])
    out = agent.propose(usage, date(2026, 11, 19), report, c, NOW, store)
    assert [(r["key"], bool(r["refused"])) for r in out] == [("dip_in_trend", False), ("peek", True), ("same_again", True)]  # 3 a night
    [saved] = agent.load(store)
    assert saved["status"] == "active" and saved["conditions"][0]["params"] == {"n": 3, "x": 15}
    assert "holdout" not in json.dumps(c.seen) and "9.99" not in json.dumps(c.seen)
    assert c.seen["group_winners"][0]["validation_sharpe"] == 0.9 and "rsi_below" in c.seen["building_blocks"]
    row = json.loads(usage.read_text().splitlines()[-1])
    assert row["purpose"] == "research" and row["usd"] == 0.2
    agent.propose(usage, date(2026, 11, 19), report, c, NOW, store)  # $0.40 so far today
    assert agent.propose(usage, date(2026, 11, 19), report, c, NOW, store) is None and c.calls == 2


def test_the_factory_tests_an_agent_rule_and_a_failure_is_retired_but_stays_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STORE", tmp_path / "agent_rules.json")
    monkeypatch.setattr(sg, "FAMILIES", {"oracle": sg.Family(
        "oracle", "o", "o", 5, lambda d, spy: d["close"].shift(-5) > d["open"].shift(-1) * 1.04, "test")})
    agent._save([_rule(key="coinflip", conds=[{"type": "up_close", "params": {}}, {"type": "narrow_range", "params": {"n": 4}}],
                       status="active", added=NOW.isoformat())])
    cands = [c for c in fy.candidates() if c["universe"] == "etfs" and c["filter"] == "any"]
    assert {c["family"] for c in cands} == {"oracle", "ag_coinflip"}
    p = _panel(6400, ("AAA", "BBB", "CCC", "SPY"), start="2000-01-03", seed=11)
    rep = fy.run({"etfs": {s: p[s] for s in ("AAA", "BBB", "CCC")}}, p["SPY"], cands, retired=agent.retired())
    assert [n.split("_")[1] for n in rep["passed"]] == ["oracle"]  # the library rule is judged apart from the agent's
    assert agent.settle(rep) == ["coinflip"]
    [r] = agent.load()
    assert r["status"] == "retired" and len(r["pvalues"]) == 1 and r["last"]
    assert list(agent.retired()) == list(r["pvalues"]) and agent.families() == {}
    assert "ag_coinflip" in agent.families(every=True)  # a book still holding its trades can rebuild the rule
    assert not any(c["family"].startswith("ag_") for c in fy.candidates())  # it is not re-run night after night
    # the library's verdicts are the same as in a run the agent never joined
    alone = fy.run({"etfs": {s: p[s] for s in ("AAA", "BBB", "CCC")}}, p["SPY"], [c for c in cands if c["family"] == "oracle"])
    assert alone["pbo"] == rep["pbo"] and alone["spa_pvalue"] == rep["spa_pvalue"]
    assert all(alone["candidates"][n]["dsr"] == rep["candidates"][n]["dsr"] for n in alone["candidates"])
    # a rule gone from the store is skipped by the trading book, not a crash
    agent._save([])
    gone = next(n for n, c in rep["candidates"].items() if c["family"] == "ag_coinflip")
    assert fy.live_strategies({**rep, "live": [gone, rep["passed"][0]]})[0].strategy.name == rep["passed"][0]


def test_a_clone_is_refused_and_a_broken_store_is_never_overwritten(tmp_path):
    store, usage = tmp_path / "agent_rules.json", tmp_path / "llm_usage.jsonl"
    first = _rule(key="rsi_dip", conds=[{"type": "rsi_below", "params": {"n": 3, "x": 15}}], status="active")
    assert agent.clone_of(_rule(key="twin", conds=[{"type": "rsi_below", "params": {"n": 3, "x": 15.5}}]), [first]) == "rsi_dip"
    assert agent.clone_of(_rule(key="other", conds=[{"type": "gap_down", "params": {"pct": 0.01}}]), [first]) is None
    assert agent.clone_of(_rule(key="breakout", conds=[{"type": "new_high", "params": {"n": 50}}]), []) == "donchian"  # a library rule renamed
    agent._save([first, {"key": "from_a_newer_version", "shape": "unknown"}], store)
    usage.write_text("not json\n")
    c = _Client([_rule(key="twin", conds=[{"type": "rsi_below", "params": {"n": 3, "x": 15.5}}]),
                 _rule(key="from_a_newer_version", conds=[{"type": "gap_down", "params": {"pct": 0.01}}])])
    out = agent.propose(usage, date(2026, 11, 19), None, c, NOW, store)
    assert "same days as rsi_dip" in out[0]["refused"] and "already used" in out[1]["refused"]
    assert len(json.loads(store.read_text())["rules"]) == 2  # the row this version cannot read is kept
    store.write_text("{ torn")
    with pytest.raises(ValueError):
        agent.propose(usage, date(2026, 11, 19), None, c, NOW, store)
    assert store.read_text() == "{ torn" and c.calls == 1  # no call, no overwrite


def test_failed_trials_raise_the_bar_for_later_agent_rules(monkeypatch):
    from ctbot.research import validation

    seen = []
    real = validation.benjamini_hochberg
    monkeypatch.setattr(validation, "benjamini_hochberg", lambda p, q=0.1: seen.append(dict(p)) or real(p, q))
    edge = lambda d, spy: pd.Series(np.arange(len(d)) % 40 == 0, d.index)  # noqa: E731
    monkeypatch.setattr(sg, "FAMILIES", {"lib": sg.Family("lib", "l", "l", 5, edge, "test"),
                                         "ag_new": sg.Family("ag_new", "a", "a", 5, edge, "test")})
    monkeypatch.setattr(agent, "families", lambda *a, **k: {})
    cands = [c for c in fy.candidates() if c["universe"] == "etfs" and c["filter"] == "any"]
    p = _panel(6400, ("AAA", "SPY"), start="2000-01-03", seed=11)
    fy.run({"etfs": {"AAA": p["AAA"]}}, p["SPY"], cands, retired={"old_fail_1": 0.9, "old_fail_2": 0.7})
    lib, ag = seen
    assert len(lib) == 1 and next(iter(lib)).startswith("fx_lib_")  # the library never sees the agent's trials
    # tonight's agent rule is ranked among the library's trials and its own failures
    assert len(ag) == 4 and {"old_fail_1", "old_fail_2", *lib} < set(ag)
