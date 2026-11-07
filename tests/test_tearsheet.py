"""ctbot-tearsheet --all: the main book, each paper book (vs main) and each factory live pick."""

import json
import sys
import types

import pandas as pd

from ctbot.library import factory as fy
from ctbot.research import tearsheet as ts


def _history(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"ts": f"2026-10-{d:02d}T20:00:00", "equity": 1000 + d}) for d in range(1, n + 1)))


def test_all_writes_books_and_factory_picks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _history(tmp_path / "data/equity_history.jsonl", 10)
    _history(tmp_path / "data/variants/fast/equity_history.jsonl", 10)
    _history(tmp_path / "data/variants/new/equity_history.jsonl", 3)  # too young: skipped
    calls = []
    fake = types.SimpleNamespace(reports=types.SimpleNamespace(html=lambda r, **k: calls.append(k)))
    monkeypatch.setitem(sys.modules, "quantstats", fake)
    monkeypatch.setattr("ctbot.variants.load", lambda: [types.SimpleNamespace(name="fast"), types.SimpleNamespace(name="new")])
    monkeypatch.setattr(fy, "load_report", lambda: {"live": ["fx_a"]})
    s = pd.Series([0.01, -0.01], index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
    monkeypatch.setattr(ts, "factory_returns", lambda name: (s, s))
    monkeypatch.setattr(sys, "argv", ["ctbot-tearsheet", "--all"])
    ts.main()
    outs = [c["output"] for c in calls]
    assert outs == ["data/reports/paper_book.html", "data/reports/book_fast.html", "data/reports/factory_fx_a.html"]
    assert calls[0]["benchmark"] is None and calls[1]["benchmark_title"] == "main book"


def test_factory_returns_start_at_the_first_trade(monkeypatch):
    from ctbot.research import factors as fx
    from tests.test_factory import _panel

    p = _panel(1500, ("SPY", "SMH", "XLK"), start="2010-01-04")
    c = next(c for c in fy.candidates() if c["universe"] == "etfs" and c["filter"] == "any")
    monkeypatch.setattr(fy, "load_report", lambda: {"candidates": {c["name"]: c}})
    monkeypatch.setattr(fx, "load_panel", lambda syms: {s: p[s] for s in syms if s in p})
    r, bench = ts.factory_returns(c["name"])
    assert r.iloc[0] != 0 and r.index.equals(bench.index)
