"""Phone updates at 9:45, 12:45 and after the close: numbers vs SPY/QQQ, re-buys, Claude's "why"."""

import json
from types import SimpleNamespace

from ctbot import brief, notify
from tests.test_readout import DAY, _at, _book, _settings


def test_slots_fire_once_each_and_a_missed_one_is_skipped():
    state = {}
    assert brief.due_slot(_at(9, 40), state) is None
    assert brief.due_slot(_at(9, 46), state) == ("Morning", DAY)
    state["Morning"] = DAY.isoformat()
    assert brief.due_slot(_at(12, 0), state) is None
    assert brief.due_slot(_at(15, 0), state) == ("Midday", DAY)
    assert brief.due_slot(_at(16, 5), state) is None  # closed, close update waits 15 min
    assert brief.due_slot(_at(16, 16), {}) == ("Close", DAY)  # stale morning/midday are not sent late
    assert brief.due_slot(_at(11, 0, 24), {}) is None  # Saturday


def test_rebought_after_a_stop_out():
    fills = [{"symbol": "PLTR", "side": "buy", "why": "mean_reversion"},
             {"symbol": "PLTR", "side": "sell", "why": "stop_loss"},
             {"symbol": "PLTR", "side": "buy", "why": "mean_reversion"},
             {"symbol": "GE", "side": "sell", "why": "eod_flatten"}, {"symbol": "GE", "side": "buy", "why": "orb"}]
    assert brief.rebought(fills) == ["PLTR"]


class _Claude:
    def __init__(self, fail=False):
        self.fail, self.seen = fail, []
        self.messages = self

    def create(self, **kw):
        if self.fail:
            raise OSError("api down")
        self.seen.append(kw)
        return SimpleNamespace(usage={"input_tokens": 500, "output_tokens": 80},
                               content=[SimpleNamespace(type="text", text="Main beat SPY on AAPL.")])


def test_update_has_numbers_market_rank_and_claude_why(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    with s.equity_history_path.open("a") as fh:
        fh.write(json.dumps({"ts": _at(15, 58).isoformat(), "equity": 10050.0, "positions": 1,
                             "market": {"SPY": -0.026, "QQQ": -0.046}}) + "\n")
    sent = []
    monkeypatch.setattr(notify, "send", lambda url, title, body: sent.append((title, body)))
    claude = _Claude()
    state = tmp_path / "brief_state.json"
    assert brief.run("https://ntfy.sh/x", s, state, _at(16, 16), claude) == "CTbot close update (paper)"
    body = sent[0][1]
    assert "Main +$50.00 (+0.50%) · SPY -2.6% · QQQ -4.6%" in body
    assert "Rank 1 of 2 books" in body and "Closed: AAPL +50 (trail)" in body
    assert body.endswith("Main beat SPY on AAPL.") and "AAPL" in claude.seen[0]["messages"][0]["content"]
    assert json.loads(s.llm_usage_path.read_text().splitlines()[-1])["purpose"] == "brief"
    assert brief.run("https://ntfy.sh/x", s, state, _at(16, 20), claude) is None  # once per slot


def test_numbers_still_go_out_when_claude_fails_or_the_cap_is_spent(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send", lambda url, title, body: sent.append(body))
    brief.run("https://ntfy.sh/x", s, tmp_path / "a.json", _at(12, 50), _Claude(fail=True))
    assert sent[-1].startswith("Main +$") and "Main beat" not in sent[-1]
    s.llm_usage_path.write_text(json.dumps({"day": DAY.isoformat(), "usd": 99}) + "\n")
    claude = _Claude()
    brief.run("https://ntfy.sh/x", s, tmp_path / "b.json", _at(12, 50), claude)
    assert claude.seen == [] and "Main beat" not in sent[-1]


def test_morning_adds_the_plan_and_no_cycle_says_check_the_bot(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / f"{DAY}.json").write_text(json.dumps({"verdict": "entries possible", "trading_models": ["orb"]}))
    sent = []
    monkeypatch.setattr(notify, "send", lambda url, title, body: sent.append(body))
    brief.run("https://ntfy.sh/x", s, tmp_path / "a.json", _at(9, 50), _Claude(), plans)
    assert "Plan: entries possible (orb)." in sent[-1]
    _book(tmp_path / "data", [(_at(15, 59, 20), 10000.0)], [])
    brief.run("https://ntfy.sh/x", s, tmp_path / "b.json", _at(9, 50), _Claude(), plans)
    assert sent[-1].startswith("No cycle finished today")
