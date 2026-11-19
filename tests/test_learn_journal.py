"""Daily learning journal: each book's day and per-model record, and the "what changed" line."""

import json
from datetime import UTC, date

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


# ---- stage 3: the learner book ----

def test_lessons_for_picks_the_reviewed_models_and_all(tmp_path):
    learn.apply(tmp_path, [_edit(), _edit(text="vwap_revert: prefer the morning", models=["vwap_revert"])], LATEST)
    assert [x["text"] for x in learn.lessons_for(tmp_path / "lessons.json", ["orb"])] == [_edit()["text"]]
    assert learn.lessons_for(tmp_path / "missing.json", ["orb"]) == []


def test_check_refuses_a_lesson_that_cannot_change_a_verdict():
    summary = {"orb": {"approved": {"n": 0}, "rejected": {"n": 40, "avg_bps": -24.0}},
               "gap": {"approved": {"n": 0}, "rejected": {"n": 40, "avg_bps": 12.0}}}
    latest = {"books": {}, "ideas": {"summary": summary}}
    assert "changes nothing" in learn.check(_edit(), [], latest)  # all rejected, and they lose
    assert learn.check(_edit(models=["gap"]), [], latest) is None  # all rejected, but they would have won


def test_scoreboard_calls_a_strategy_losing_only_with_a_sample_and_significance():
    def g(n, avg, t):
        return {"approved": {"n": n, "avg_bps": avg, "t": t}, "rejected": {"n": 0}}

    board = learn.scoreboard({"all": g(200, -5, -1), "bad": g(40, -30.0, -3.5), "noisy": g(40, -30.0, -0.8),
                              "thin": g(8, -90.0, -4.0), "good": g(40, 25.0, 2.0), "unreviewed": {"rejected": {"n": 3}}})
    assert set(board) == {"bad", "noisy", "thin", "good"}
    assert [m for m, r in board.items() if r["losing"]] == ["bad"]
    assert board["thin"]["p"] is None and board["bad"]["p"] < 0.001


def _paired_idea(ts, take, plain_take, lessons=("L1",)):
    return {"id": ts, "ts": ts, "symbol": "KO", "direction": "long",
            "decision": {"approve": take, "confidence": 0.5,
                         "paired": {"lessons": list(lessons), "take": take, "plain_take": plain_take}},
            "signals": [{"strategy": "orb", "direction": "long",
                         "evidence": {"stop": 98.0, "target": 102.0, "exit_at": "2026-10-13T15:50:00-04:00"}}]}


def test_lesson_effects_score_only_the_verdicts_a_lesson_changed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from ctbot.models import Bar
    from ctbot.research import bars

    t0 = datetime(2026, 10, 13, 15, tzinfo=UTC)
    down = [Bar("KO", t0 + timedelta(minutes=5 * i), 100, 100, 97, 97, 1) for i in range(80)]  # every entry stops out
    monkeypatch.setattr(bars, "load", lambda interval, sym: down)
    ts = [(t0 + timedelta(minutes=5 * i)).isoformat() for i in range(4)]
    ideas = [_paired_idea(ts[0], False, True),   # the lesson made the bot pass on a loser: good
             _paired_idea(ts[1], True, False),   # the lesson made the bot buy a loser: bad
             _paired_idea(ts[2], False, True),
             _paired_idea(ts[3], True, True)]    # same verdict either way: not counted
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("\n".join(json.dumps(x) for x in ideas))
    b.write_text(json.dumps(ideas[0]))  # the same review in a second book counts once
    e = learn.lesson_effects([a, b, tmp_path / "missing.jsonl"])["L1"]
    assert e["n"] == 3 and e["avg_bps"] > 0 and round(e["win_rate"], 2) == 0.67


def test_guard_deletes_a_lesson_whose_changed_verdicts_lost(tmp_path):
    learn.apply(tmp_path, [_edit(), _edit(text="orb: skip after 15:00")], LATEST)
    keep, bad = (x["id"] for x in learn.load_lessons(tmp_path))
    effects = {keep: {"n": 25, "avg_bps": 4.0}, bad: {"n": 19, "avg_bps": -30.0}, "gone": {"n": 50, "avg_bps": -9.0}}
    assert learn.guard(tmp_path, {**LATEST, "lessons": effects}) == []  # 19 changed verdicts: too few to judge
    effects[bad]["n"] = 20
    row, = learn.guard(tmp_path, {**LATEST, "lessons": effects})
    assert row["trigger"] == "guard" and row["action"] == "delete" and not row["refused"]
    assert [x["id"] for x in learn.load_lessons(tmp_path)] == [keep] and learn.verify(tmp_path)
    learn.rollback(tmp_path, row["event"])  # a person can bring it back
    assert len(learn.load_lessons(tmp_path)) == 2
