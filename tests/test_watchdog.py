"""ctbot-watchdog: healthy only when the bot is alive and (in market hours) finishing cycles."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ctbot import watchdog

ET = ZoneInfo("America/New_York")
OPEN_DAY = datetime(2026, 10, 21, 11, 0, tzinfo=ET).astimezone(timezone.utc)  # a Wednesday, 11:00 ET
SATURDAY = datetime(2026, 10, 24, 11, 0, tzinfo=ET).astimezone(timezone.utc)


def _files(tmp_path, now, beat_age_s=5, equity_age_s=60, log_lines=()):
    hb, eq, log = tmp_path / "paper_heartbeat.json", tmp_path / "equity_history.jsonl", tmp_path / "paper.log"
    if beat_age_s is not None:
        hb.write_text(json.dumps({"ts": (now - timedelta(seconds=beat_age_s)).isoformat(), "interval": 300}))
    if equity_age_s is not None:
        eq.write_text(json.dumps({"ts": (now - timedelta(seconds=equity_age_s)).isoformat(), "equity": 1}) + "\n")
    log.write_text("\n".join(log_lines))
    return hb, eq, log


def test_healthy_when_heartbeat_and_cycles_are_fresh(tmp_path):
    ok, reason = watchdog.check(*_files(tmp_path, OPEN_DAY), now=OPEN_DAY)
    assert ok and "market open" in reason


def test_missing_or_stale_heartbeat_fails(tmp_path):
    ok, reason = watchdog.check(*_files(tmp_path, OPEN_DAY, beat_age_s=None), now=OPEN_DAY)
    assert not ok and "not running" in reason
    ok, reason = watchdog.check(*_files(tmp_path, OPEN_DAY, beat_age_s=600), now=OPEN_DAY)
    assert not ok and "600s" in reason


def test_cycles_not_finishing_during_market_hours_fails(tmp_path):
    ok, reason = watchdog.check(*_files(tmp_path, OPEN_DAY, equity_age_s=3600), now=OPEN_DAY)
    assert not ok and "cycles not finishing" in reason
    ok, _ = watchdog.check(*_files(tmp_path, OPEN_DAY, equity_age_s=None), now=OPEN_DAY)
    assert not ok


def test_first_minutes_after_open_and_closed_market_do_not_need_new_cycles(tmp_path):
    just_opened = datetime(2026, 10, 21, 9, 40, tzinfo=ET).astimezone(timezone.utc)
    assert watchdog.check(*_files(tmp_path, just_opened, equity_age_s=17 * 3600), now=just_opened)[0]
    ok, reason = watchdog.check(*_files(tmp_path, SATURDAY, equity_age_s=2 * 86400), now=SATURDAY)
    assert ok and "market closed" in reason


def test_robinhood_sign_in_needed_in_the_log_fails(tmp_path):
    lines = ["2026-10-21 10:59:00 ERROR cycle failed; continuing",
             "ctbot.market_data.robinhood_mcp_client.RobinhoodLoginRequired: Robinhood sign-in needed: run ..."]
    ok, reason = watchdog.check(*_files(tmp_path, SATURDAY, log_lines=lines), now=SATURDAY)
    assert not ok and "sign-in" in reason


def test_ping_hits_the_url_or_its_fail_endpoint(monkeypatch):
    import httpx

    calls = []
    monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append(("GET", url, None)))
    monkeypatch.setattr(httpx, "post", lambda url, content=None, **kw: calls.append(("POST", url, content)))
    watchdog.ping("https://hc-ping.com/abc/", True, "ok")
    watchdog.ping("https://hc-ping.com/abc", False, "paper bot not running")
    assert calls == [("GET", "https://hc-ping.com/abc", None),
                     ("POST", "https://hc-ping.com/abc/fail", b"paper bot not running")]
