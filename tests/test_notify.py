"""Phone notifications: one open message, one close summary per session, and health changes."""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ctbot import notify

ET = ZoneInfo("America/New_York")


def _at(h, m, day=21):  # 2026-10-21 is a Wednesday
    return datetime(2026, 10, day, h, m, tzinfo=ET).astimezone(timezone.utc)


def _setup(tmp_path, equity_rows, fills=()):
    eq = tmp_path / "equity_history.jsonl"
    eq.write_text("".join(json.dumps({"ts": ts.isoformat(), "equity": e}) + "\n" for ts, e in equity_rows))
    ledger = tmp_path / "paper_ledger.json"
    ledger.write_text(json.dumps({"fills": [{"ts": ts.isoformat()} for ts in fills]}))
    plans = tmp_path / "day_plan"
    plans.mkdir()
    (plans / "2026-10-21.json").write_text(json.dumps({"verdict": "entries possible", "trading_models": ["orb"]}))
    return eq, ledger, plans


def test_open_message_once_after_the_first_cycle_then_a_close_summary(tmp_path):
    eq, ledger, plans = _setup(tmp_path, [(_at(16, 0, 20), 1000.0), (_at(9, 31), 1001.0), (_at(15, 59), 1012.5)],
                               fills=[_at(10, 0), _at(11, 0), _at(16, 0, 20)])
    state = {}
    assert notify.due(state, _at(9, 20), True, "ok", eq, ledger, plans) == []
    msgs = notify.due(state, _at(9, 32), True, "ok", eq, ledger, plans)
    assert [m[0] for m in msgs] == ["open"]
    assert "CTbot is trading" in msgs[0][1] and "entries possible (orb)" in msgs[0][2] and "$1,001.00" in msgs[0][2]
    assert notify.due(state, _at(10, 0), True, "ok", eq, ledger, plans) == []  # once per day
    assert notify.due(state, _at(16, 5), True, "ok", eq, ledger, plans) == []  # waits for the last cycle
    msgs = notify.due(state, _at(16, 16), True, "ok", eq, ledger, plans)
    assert [m[0] for m in msgs] == ["close"]
    assert "+12.50" in msgs[0][2] and "+1.25%" in msgs[0][2] and "Fills today: 2" in msgs[0][2]
    assert notify.due(state, _at(17, 0), True, "ok", eq, ledger, plans) == []


def test_health_changes_are_sent_once_each_and_weekends_stay_quiet(tmp_path):
    eq, ledger, plans = _setup(tmp_path, [])
    state = {}
    saturday = _at(11, 0, 24)
    assert notify.due(state, saturday, True, "ok", eq, ledger, plans) == []
    msgs = notify.due(state, saturday, False, "paper bot not running", eq, ledger, plans)
    assert msgs == [("healthy", "CTbot problem", "paper bot not running")]
    assert notify.due(state, saturday, False, "paper bot not running", eq, ledger, plans) == []
    assert notify.due(state, saturday, True, "ok", eq, ledger, plans)[0][1] == "CTbot recovered"


def test_a_failed_send_is_retried_on_the_next_run(tmp_path, monkeypatch):
    eq, ledger, plans = _setup(tmp_path, [(_at(9, 31), 1000.0)])
    sent, fail = [], [True]

    def fake_send(url, title, body):
        if fail[0]:
            raise OSError("offline")
        sent.append(title)

    monkeypatch.setattr(notify, "send", fake_send)
    state_path = tmp_path / "notify_state.json"
    assert notify.run("https://ntfy.sh/x", state_path, _at(9, 32), True, "ok", eq, ledger, plans) == []
    fail[0] = False
    assert notify.run("https://ntfy.sh/x", state_path, _at(9, 33), True, "ok", eq, ledger, plans) == [
        "Market open: CTbot is trading (paper)"]
