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
