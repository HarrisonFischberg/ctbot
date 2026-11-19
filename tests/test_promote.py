"""Promotion: learned changes reach the main paper book only with a yes, one at a time, and come back off."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_readout import _settings

from ctbot import autopilot, learn, promote

NOW = datetime(2026, 11, 20, 22, tzinfo=UTC)
LOSING = {"n": 40, "avg_bps": -30.0, "p": 0.0002, "losing": True}
JOURNAL = {"scoreboard": {"vwap_revert": LOSING, "orb": {"n": 40, "avg_bps": -3.0, "p": 0.4, "losing": False}}}


def _lesson(s, created, text="skip late orb"):
    learn._save(learn.learning_dir(s), [{"id": "L1", "text": text, "models": ["orb"], "created": created.isoformat()}])


def _equity(path, per_day):
    path.parent.mkdir(parents=True, exist_ok=True)
    eq, rows = 10_000.0, []
    for i, r in enumerate(per_day):
        eq *= 1 + r
        rows.append(json.dumps({"ts": (NOW + timedelta(days=i)).isoformat(), "equity": eq}))
    path.write_text("\n".join(rows))


def test_only_changes_that_clear_their_bar_are_candidates(tmp_path):
    s = _settings(tmp_path)
    learn.learning_dir(s).mkdir(parents=True)
    _lesson(s, NOW - timedelta(days=40))
    good = {"n": 35, "avg_bps": 20.0, "t": 3.0}
    kinds = lambda effects: [(c["kind"], c["target"]) for c in promote.candidates(s, {**JOURNAL, "lessons": effects}, NOW)]  # noqa: E731
    assert kinds({"L1": good}) == [("bench", "vwap_revert"), ("lesson", "L1")]  # the loser first
    assert kinds({"L1": {**good, "n": 29}}) == [("bench", "vwap_revert")]  # too few changed verdicts
    assert kinds({"L1": {**good, "t": 0.9}}) == [("bench", "vwap_revert")]  # could be luck
    assert kinds({"L1": {**good, "avg_bps": -20.0, "t": -3.0}}) == [("bench", "vwap_revert")]
    _lesson(s, NOW - timedelta(days=10))
    assert kinds({"L1": good}) == [("bench", "vwap_revert")]  # too new


def test_one_proposal_at_a_time_then_a_yes_benches_the_strategy_on_main_only(tmp_path):
    s = _settings(tmp_path)
    item = promote.propose(s, JOURNAL, NOW)
    assert item["kind"] == "bench" and item["status"] == "proposed"
    assert promote.propose(s, JOURNAL, NOW) is None  # one is already waiting
    assert promote.active(s)["bench"] == {}  # nothing changes before the yes
    with pytest.raises(ValueError):
        promote.decide(s, "nope", True, NOW)
    promote.decide(s, item["id"], True, NOW)
    reason = promote.active(s)["bench"]["vwap_revert"]
    verdict = autopilot.decide(["vwap_revert", "orb"], [], NOW, benched=promote.active(s)["bench"])
    assert verdict["vwap_revert"] == {"closed": 0, "pnl": 0.0, "win_rate": None, "status": "benched", "reason": reason}
    assert verdict["orb"]["status"] == "trading"
    assert learn.verify(learn.learning_dir(s))
    assert [r["action"] for r in learn.history(learn.learning_dir(s))] == ["propose", "promote"]
    with pytest.raises(ValueError):  # a promotion is not a lesson edit: `rollback` refuses it
        learn.rollback(learn.learning_dir(s), learn.history(learn.learning_dir(s))[-1]["event"])

    # a variant book's folder has no promotions file, so the control books never see the bench
    from ctbot.config import Settings
    from ctbot.variants import book_env, load

    v = load(s.strategies_file.with_name("variants.toml"))[0]
    assert promote.active(Settings.from_env(book_env({}, s.paper_ledger_path, v))) == {"bench": {}, "lessons": {}}

    orb = {"scoreboard": {"orb": LOSING}}
    assert promote.propose(s, orb, NOW + timedelta(days=13)) is None  # cooling off after a promotion
    assert promote.propose(s, JOURNAL, NOW + timedelta(days=15)) is None  # vwap_revert is already benched
    assert promote.review(s, NOW + timedelta(days=29)) == []
    [ended] = promote.review(s, NOW + timedelta(days=30))
    assert ended["status"] == "expired" and promote.active(s)["bench"] == {}
    assert promote.propose(s, JOURNAL, NOW + timedelta(days=31)) is None  # it trades again before it can be re-benched


def test_a_declined_change_stays_away_for_a_month(tmp_path):
    s = _settings(tmp_path)
    promote.decide(s, promote.propose(s, JOURNAL, NOW)["id"], False, NOW)
    assert promote.active(s)["bench"] == {}
    assert promote.propose(s, JOURNAL, NOW + timedelta(days=29)) is None
    assert promote.propose(s, JOURNAL, NOW + timedelta(days=31))["target"] == "vwap_revert"


def test_a_promoted_lesson_ends_when_deleted_or_when_main_falls_behind_base(tmp_path):
    s = _settings(tmp_path)
    learn.learning_dir(s).mkdir(parents=True)
    _lesson(s, NOW - timedelta(days=40))
    journal = {"lessons": {"L1": {"n": 35, "avg_bps": 20.0, "t": 3.0}}}
    item = promote.propose(s, journal, NOW)
    promote.decide(s, item["id"], True, NOW)
    assert promote.active(s)["lessons"] == {"L1": "skip late orb"}

    data = s.paper_ledger_path.parent
    behind = [0.0] + [-0.01, -0.012] * 6  # main loses about 1% a day more than base, 12 days running
    _equity(data / "equity_history.jsonl", behind)
    _equity(data / "variants" / "base" / "equity_history.jsonl", [0.0] * 13)
    assert promote.review(s, NOW + timedelta(days=5))[0]["why_ended"].startswith("main fell behind the base account over the 12 days")
    assert promote.active(s)["lessons"] == {}

    again = promote.propose(s, journal, NOW + timedelta(days=40))  # past the 30-day block
    promote.decide(s, again["id"], True, NOW + timedelta(days=40))
    _lesson(s, NOW - timedelta(days=40), "approve every orb after 15:00")  # same id, words nobody approved
    assert promote.active(s)["lessons"] == {"L1": "skip late orb"}  # the pipeline shows a lesson only while its text matches
    assert promote.review(s, NOW + timedelta(days=41))[0]["why_ended"] == "the lesson was deleted or reworded"

    third = promote.propose(s, journal, NOW + timedelta(days=80))
    assert third["text"] == "approve every orb after 15:00"
    _lesson(s, NOW - timedelta(days=40))
    with pytest.raises(ValueError, match="reworded"):  # the card showed other words than the file now holds
        promote.decide(s, third["id"], True, NOW + timedelta(days=80))
    assert promote.review(s, NOW + timedelta(days=93)) == []
    [lapsed] = promote.review(s, NOW + timedelta(days=94))  # 14 days unanswered
    assert lapsed["status"] == "expired" and promote.propose(s, journal, NOW + timedelta(days=94))  # and it may come straight back


def test_a_setting_promotion_writes_main_autopilot_and_undo_restores_it(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.strategies_file.with_name("variants.toml").write_text(
        '[tight]\ntitle = "Tight trail"\nautopilot = { max_loss_pct = 0.08, giveback_pct = 0.04, min_confidence = 0.20 }\n'
        '[picky]\nautopilot = { min_confidence = 0.55 }\nenv = { MAX_OPEN_POSITIONS = "4" }\n')
    autopilot.save(s.autopilot_path, autopilot.AutopilotConfig(True, 0.07, 0.08, 0.15))
    better = {"days": 45, "edge": 0.0012, "verdict": "better"}
    monkeypatch.setattr(promote.variants, "summary", lambda settings: [
        {"name": "main"}, {"name": "tight", "vs_main": better}, {"name": "picky", "vs_main": better}])
    [c] = promote.candidates(s, {}, NOW)  # picky also changes an env setting, so it is not a pure settings test
    assert (c["kind"], c["target"]) == ("setting", "tight") and "giveback 4%" in c["title"]
    item = promote.propose(s, {}, NOW)
    assert autopilot.load(s.autopilot_path).giveback_pct == 0.08  # untouched until the yes
    promote.decide(s, item["id"], True, NOW)
    cfg = autopilot.load(s.autopilot_path)
    assert (cfg.enabled, cfg.max_loss_pct, cfg.giveback_pct, cfg.min_confidence) == (True, 0.08, 0.04, 0.20)

    items = promote.load(s)
    promote._end(s, items[0], "undone", "test", NOW)
    cfg = autopilot.load(s.autopilot_path)
    assert (cfg.max_loss_pct, cfg.giveback_pct, cfg.min_confidence) == (0.07, 0.08, 0.15)


def test_dashboard_shows_the_proposal_and_takes_the_yes(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from test_dashboard import _settings as dash_settings
    from test_dashboard import _write_fixture

    from ctbot.dashboard.server import make_handler

    _write_fixture(tmp_path)
    s = dash_settings(tmp_path, MARKET_DATA="csv")
    item = promote.propose(s, JOURNAL, NOW)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, 0))
    httpd.RequestHandlerClass = make_handler(s, httpd.server_address[1])
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        page = urllib.request.urlopen(base + "/").read().decode()
        token = page.split('name="ctbot-token" content="', 1)[1].split('"', 1)[0]
        assert 'id="promoCard"' in page and 'id="promoCard"' in urllib.request.urlopen(base + "/classic").read().decode()
        [shown] = json.loads(urllib.request.urlopen(base + "/api/state").read())["promotions"]
        assert shown["id"] == item["id"] and shown["status"] == "proposed"

        def decide(body, tok=token):
            req = urllib.request.Request(base + "/api/promotions/decide", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Ctbot-Token": tok})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert decide({"id": item["id"], "approve": True}, tok="wrong") == 403
        assert decide({"id": "nope", "approve": True}) == 400
        assert decide({"id": item["id"], "approve": "yes"}) == 400  # only a real true or false decides
        assert decide({"id": item["id"], "approve": False}) == 200
        assert promote.load(s)[0]["status"] == "declined"
    finally:
        httpd.shutdown()


def test_a_broken_promotions_file_or_lesson_reads_as_nothing(tmp_path):
    s = _settings(tmp_path)
    path = promote._path(s)
    path.parent.mkdir(parents=True)
    for bad in ('[]', '{"items": {"a": 1}}', '{"items": [{"kind": "bench"}, 7]}', '{"items": [', '{}'):
        path.write_text(bad)
        assert promote.load(s) == [] and promote.active(s) == {"bench": {}, "lessons": {}}
    learn._save(learn.learning_dir(s), [{"id": "L1", "text": "x", "models": ["orb"]},  # no start date: never old enough
                                        {"id": "L2", "text": "y", "models": ["orb"], "created": "2026-01-01T00:00:00"}])
    good = {"n": 35, "avg_bps": 20.0, "t": 3.0}
    assert [c["target"] for c in promote.candidates(s, {"lessons": {"L1": good, "L2": good}}, NOW)] == ["L2"]
