"""The 96 index rules: scored nightly on forward trades, promoted only past a multiple-testing bar."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest
from test_readout import _settings

from ctbot import promote
from ctbot.library import index_rules as ix

TODAY = date(2028, 6, 1)


def _bars(seed, n=7500, drift=0.0003):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2028-05-31", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.012, n)))
    op = np.roll(close, 1) * np.exp(rng.normal(0, 0.003, n))
    op[0] = close[0]
    return pd.DataFrame({"open": op, "high": np.maximum(op, close), "low": np.minimum(op, close), "close": close,
                         "volume": 1e6}, index=idx)


def test_there_are_96_rules_and_none_looks_ahead():
    assert len(ix.NAMES) == len(set(ix.NAMES)) == 96 and ix.parts("ix_QQQ_three_down_below_h5") == ("QQQ", "three_down", "below", 5)
    assert ix.title("ix_QQQ_rsi5_below_h5") == "buy QQQ after it has 2-day RSI under 5 while QQQ is below its 170-day average, hold 5 days"
    spy, qqq = _bars(1), _bars(2)
    for name in ix.NAMES:
        d = spy if ix.parts(name)[0] == "SPY" else qqq
        full = ix.fires(name, d, qqq)
        assert (ix.fires(name, d.iloc[:5000], qqq.iloc[:5000]) == full.iloc[:5000]).all(), name


def test_a_trade_is_next_open_to_the_close_hold_days_after_the_signal_and_never_overlaps():
    d = _bars(3, n=30)
    sig = pd.Series(False, d.index)
    sig.iloc[[4, 6, 12, 28]] = True  # day 6 falls inside the first trade; day 28 has no exit bar yet
    t = ix._trades(d, sig, 5)
    assert list(t.index) == [d.index[5], d.index[13]]
    assert t.iloc[0] == pytest.approx(d["close"].iloc[9] / d["open"].iloc[5] - 1)


def test_noise_promotes_nothing_and_a_real_forward_edge_is_winning_only_past_the_bar():
    spy, qqq = _bars(1), _bars(2)
    rep = ix.score({"SPY": spy, "QQQ": qqq}, TODAY)
    assert len(rep["rules"]) == 96 and not any(r["winning"] for r in rep["rules"].values())
    for seed in range(3, 9):  # more made-up markets with no edge
        assert not any(r["winning"] for r in ix.score({"SPY": _bars(seed), "QQQ": _bars(seed + 50)}, TODAY)["rules"].values())
    name = "ix_QQQ_three_down_any_h1"
    f = ix.fires(name, qqq, qqq)
    days = np.flatnonzero((f & (qqq.index >= ix.FORWARD_FROM)).to_numpy()[:-1]) + 1
    rigged = qqq.copy()  # every forward trade of this one rule gains 1-2% from open to close
    rigged.iloc[days, rigged.columns.get_loc("open")] = rigged["close"].iloc[days] / (1.01 + 0.01 * (days % 2))
    rep = ix.score({"SPY": spy, "QQQ": rigged}, TODAY)
    row = rep["rules"][name]
    assert row["winning"] and row["forward"]["n"] >= ix.LOOKS[0] and row["forward"]["avg"] > 0.01
    assert len(row["trades"]) == row["forward"]["n"] and row["trades"][0][0] >= ix.FORWARD_FROM
    assert [n for n, r in rep["rules"].items() if r["history_ok"]] == sorted(ix.PRIOR, key=ix.NAMES.index)
    assert not ix.score({"SPY": spy, "QQQ": rigged}, date(2026, 11, 19))["rules"][name]["winning"]  # too soon
    few = rigged[rigged.index <= rigged.index[days[5]]]  # only a handful of forward trades: not yet
    assert not ix.score({"SPY": spy[spy.index <= few.index[-1]], "QQQ": few}, TODAY)["rules"][name]["winning"]


def test_a_winning_rule_is_promoted_and_comes_off_when_its_record_turns(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(ix, "REPORT", tmp_path / "index_rules.json")
    name, now = "ix_QQQ_rsi5_below_h5", datetime(2027, 6, 1, 22, tzinfo=UTC)
    row = {"forward": {"n": 15, "avg": 0.012, "excess": 0.01, "t": 3.4}, "p": 0.002, "history_ok": False, "winning": True,
           "trades": [["2027-05-03", 0.012]]}

    def save(rows, bars="2027-06-01"):
        ix.REPORT.write_text(json.dumps({"bars_through": bars, "rules": rows}))

    twin, blocked = {**row, "p": 0.01}, {**row, "p": 0.0001}
    save({name: row}, bars="2027-05-20")
    assert promote.candidates(s, {}, now) == []  # the bars stopped refreshing: an old report proposes nothing
    save({name: row, "not_a_rule": row, "ix_QQQ_rsi10_below_h5": twin, "ix_QQQ_rsi5_above_h5": blocked,
          "ix_SPY_dd7_any_h1": {"winning": True}})  # a row that is not whole is ignored, not a crash
    [c] = promote.candidates(s, {}, now)  # one rule a fund, and never one the QQQ core would block
    assert c["target"] == name
    item = promote.propose(s, {}, now, approve=True)
    assert (item["kind"], item["target"], item["status"]) == ("rule", name, "active") and "96" in item["evidence"]
    assert promote.active(s)["rules"] == {name} and promote.candidates(s, {}, now) == []
    [ws] = ix.live_strategies(promote.active(s)["rules"] | {"fx_some_factory_rule"})
    assert ws.strategy.name == name and ws.mode == "trade"
    row["winning"], row["forward"]["avg"] = False, 0.002  # no longer significant, still positive: it stays
    row["trades"] += [["2027-06-02", -0.01], ["2027-06-10", -0.01]]
    save({name: row})
    assert promote.review(s, now) == []  # two losing trades since the promotion are not yet a verdict
    row["trades"] += [["2027-06-20", 0.005]]
    save({name: row})
    [ended] = promote.review(s, now)  # three that lost on average are, though the whole record is still up
    assert ended["status"] == "undone" and "stopped winning" in ended["why_ended"] and promote.active(s)["rules"] == frozenset()


def test_the_live_rule_buys_once_at_the_open_with_a_stop_and_a_timed_exit():
    from zoneinfo import ZoneInfo

    qqq = _bars(2, n=600)
    name = "ix_QQQ_three_down_any_h5"
    f = ix.fires(name, qqq, qqq)
    day = next(i for i in range(400, 599) if f.iloc[i])
    today = qqq.index[day + 1].date()
    bars = [NS(ts=datetime.combine(t.date(), datetime.min.time(), tzinfo=UTC), **{k: float(r[k]) for k in ("open", "high", "low", "close", "volume")})
            for t, r in qqq.iterrows()]
    md = NS(daily_bars=lambda sym, n: bars, quotes=lambda syms: {"QQQ": NS(last=200.0)})
    now = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo("America/New_York")).replace(hour=9, minute=40)
    ctx = NS(market_data=md, universe=(), events=(), today=today, held_symbols=frozenset(), now=now)
    rule = ix.IndexRule(name)
    [sig] = rule.generate(ctx)
    assert sig.symbol == "QQQ" and sig.evidence["stop"] == 184.0 and sig.evidence["systematic"]
    assert datetime.fromisoformat(sig.evidence["exit_at"]).date() == qqq.index[day + 5].date()  # the replay's exit bar
    [one] = ix.IndexRule("ix_QQQ_three_down_any_h1").generate(ctx)
    assert datetime.fromisoformat(one.evidence["exit_at"]).date() == today and one.evidence["size_pct"] == 1.0
    assert rule.generate(ctx) == []  # once a day
    assert ix.IndexRule(name).generate(NS(**{**vars(ctx), "held_symbols": frozenset({"QQQ"})})) == []
