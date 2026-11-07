"""Phone notifications: health changes, once per change (the daily updates are tests/test_brief.py)."""

from ctbot import notify


def test_health_changes_are_sent_once_each():
    state = {}
    assert notify.due(state, True, "ok") == []
    assert notify.due(state, False, "paper bot not running") == [("healthy", "CTbot problem", "paper bot not running")]
    assert notify.due(state, False, "paper bot not running") == []
    assert notify.due(state, True, "ok")[0][1] == "CTbot recovered"


def test_a_failed_send_is_retried_on_the_next_run(tmp_path, monkeypatch):
    sent, fail = [], [True]

    def fake_send(url, title, body):
        if fail[0]:
            raise OSError("offline")
        sent.append(title)

    monkeypatch.setattr(notify, "send", fake_send)
    state_path = tmp_path / "notify_state.json"
    state_path.write_text('{"healthy": true}')
    assert notify.run("https://ntfy.sh/x", state_path, False, "down") == []
    fail[0] = False
    assert notify.run("https://ntfy.sh/x", state_path, False, "down") == ["CTbot problem"]
    assert notify.run("https://ntfy.sh/x", state_path, False, "down") == []
