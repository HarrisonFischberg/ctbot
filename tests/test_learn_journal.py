"""Daily learning journal: each book's day and per-model record, and the "what changed" line."""

import json
from datetime import date

from test_readout import DAY, _settings

from ctbot import learn, readout

FACTORY = {"generated": "2026-10-21", "n_candidates": 1080, "passed": [1, 2], "live": ["calm_trend_stocks"]}


def test_journal_records_books_ideas_and_factory_then_reports_changes(tmp_path):
    s = _settings(tmp_path)
    s.ideas_path.with_name("idea_scores.json").write_text(json.dumps(
        {"generated_at": "2026-10-21T21:00:00", "scored": 3, "summary": {"all": {}}, "calibration": [], "scores": []}))
    j = learn.journal(s, DAY, FACTORY)
    main, tight = j["books"]["main"], j["books"]["tight"]
    assert main["day_pnl"] == 50.0 and main["closed_today"] == 1 and main["wins_today"] == 1
    assert main["models"]["orb"] == {"closed": 1, "win_rate": 1.0, "avg_ret": 0.05, "pnl": 50.0}
    assert tight["models"]["orb"]["closed"] == 1 and tight["wins_today"] == 0
    assert j["ideas"]["scored"] == 3 and "scores" not in j["ideas"]
    assert j["factory"] == {"generated": "2026-10-21", "n_candidates": 1080, "passed": 2, "live": ["calm_trend_stocks"]}
    assert learn.journal(s, date(2026, 10, 20), FACTORY)["books"]["main"]["models"] == {}  # no future fills

    prev = {"books": {"main": {"models": {}}}, "factory": {"live": ["golden_cross_stocks"]}}
    assert learn.changes(prev, j) == ["orb: 1 more closed, win rate now 100% over 1",
                                      "factory added calm_trend_stocks", "factory dropped golden_cross_stocks"]


def test_write_and_previous_skip_the_same_day(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(learn, "journal", lambda settings, day: {"day": day.isoformat()})
    learn.write(s, date(2026, 10, 20))
    path = learn.write(s, DAY)
    assert path == learn.journal_dir(s) / "2026-10-21.json"
    assert learn.previous(path.parent, DAY) == {"day": "2026-10-20"}


def test_readout_has_a_what_changed_line(tmp_path, monkeypatch):
    from ctbot.library import factory

    monkeypatch.setattr(factory, "load_report", lambda path=None: None)
    assert "What changed: orb: 1 more closed, win rate now 100% over 1" in readout.build(_settings(tmp_path), DAY)


# ---- stage 2: lessons ----

LATEST = {"books": {"main": {"models": {"orb": {"closed": 20}, "vwap_revert": {"closed": 3}}}},
          "ideas": {"summary": {"vwap_revert": {"approved": {"n": 10}, "rejected": {"n": 25}}}}}


def _edit(text="orb ideas approved late in the day lost in 9 of 11; ask for a strong reason", models=("orb",), **kw):
    return {"action": "create", "id": None, "text": text, "models": list(models), "evidence": "orb 20 closed", **kw}


def test_check_refuses_thin_samples_risk_wording_and_unknown_ids():
    assert learn.check(_edit(), [], LATEST) is None
    assert learn.check(_edit(models=["vwap_revert"]), [], LATEST) is None  # 35 scored ideas
    assert "below" in learn.check(_edit(models=["overnight"]), [], LATEST)
    assert "sizes" in learn.check(_edit(text="Size up orb: raise MAX_POSITION_PCT"), [], LATEST)
    assert "sizes" in learn.check(_edit(text="widen the stop-loss on orb"), [], LATEST)
    assert "sizes" in learn.check(_edit(text="orb is ready for live trading"), [], LATEST)
    assert learn.check(_edit(text="x" * 301), [], LATEST).startswith("text")
    assert learn.check({"action": "update", "id": "nope"}, [], LATEST) == "no such lesson"
    full = [{"id": str(i)} for i in range(learn.MAX_LESSONS)]
    assert "already" in learn.check(_edit(), full, LATEST)


def test_apply_logs_a_hash_chain_and_rollback_restores(tmp_path):
    rows = learn.apply(tmp_path, [_edit(), _edit(models=["overnight"]), _edit(), _edit()], LATEST)
    assert len(rows) == learn.MAX_EDITS and rows[1]["refused"] and not rows[0]["refused"]
    lessons = learn.load_lessons(tmp_path)
    assert len(lessons) == 2 and learn.verify(tmp_path)
    lid = lessons[0]["id"]
    up = learn.apply(tmp_path, [{"action": "update", "id": lid, "text": "orb: stricter after 15:00",
                                  "models": ["orb"], "evidence": "orb 20 closed"}], LATEST)[0]
    assert next(x for x in learn.load_lessons(tmp_path) if x["id"] == lid)["text"] == "orb: stricter after 15:00"
    learn.rollback(tmp_path, up["event"])
    assert next(x for x in learn.load_lessons(tmp_path) if x["id"] == lid)["text"] == lessons[0]["text"]
    learn.rollback(tmp_path, rows[0]["event"])  # undo the create
    assert lid not in {x["id"] for x in learn.load_lessons(tmp_path)} and learn.verify(tmp_path)
    (tmp_path / "history.jsonl").write_text((tmp_path / "history.jsonl").read_text().replace("stricter", "looser"))
    assert not learn.verify(tmp_path)


class _Client:
    def __init__(self, edits):
        self.calls, self.edits = 0, edits
        self.messages = self

    def create(self, **kw):
        from types import SimpleNamespace as NS

        self.calls += 1
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"edits": self.edits}))],
                  usage={"input_tokens": 1_000_000, "output_tokens": 0})


def test_refine_applies_checked_edits_records_spend_and_stops_at_the_daily_cap(tmp_path):
    s = _settings(tmp_path)
    assert learn.refine(s, DAY, _Client([])) is None  # no journal yet
    folder = learn.journal_dir(s)
    folder.mkdir(parents=True)
    (folder / "2026-10-21.json").write_text(json.dumps({"day": "2026-10-21", **LATEST, "factory": {"live": []}}))
    c = _Client([_edit()])
    rows = learn.refine(s, DAY, c, now=readout._session(DAY)[1])
    assert len(rows) == 1 and not rows[0]["refused"] and len(learn.load_lessons(learn.learning_dir(s))) == 1
    row = json.loads(s.llm_usage_path.read_text().splitlines()[-1])
    assert row["purpose"] == "learn" and row["day"] == "2026-10-21" and row["usd"] == 2.0
    assert learn.refine(s, DAY, c) is None and c.calls == 1  # $2 is past the $0.25 cap
