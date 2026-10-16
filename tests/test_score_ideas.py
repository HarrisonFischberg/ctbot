"""After-the-fact scoring of reviewed ideas (approved vs rejected)."""

from datetime import datetime, timedelta, timezone

from ctbot.models import Bar
from ctbot.research.score_ideas import score_idea, summarize

T0 = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)  # 11:00 ET


def _bars(path):
    return [Bar("KO", T0 + timedelta(minutes=5 * i), o, max(o, c), min(o, c), c, 1) for i, (o, c) in enumerate(path)]


def _idea(approve, stop=98.0, target=102.0, exit_at="2026-10-13T15:50:00-04:00"):
    return {"id": "x", "ts": T0.isoformat(), "symbol": "KO", "direction": "long",
            "decision": {"approve": approve, "confidence": 0.5},
            "signals": [{"strategy": "orb", "direction": "long",
                         "evidence": {"stop": stop, "target": target, "exit_at": exit_at}}]}


def test_target_stop_and_time_exits():
    up = _bars([(100, 101), (101, 102.5)] + [(102.5, 102.5)] * 80)
    s = score_idea(_idea(True), up)
    assert s["exit_reason"] == "target" and s["exit"] == 102.0 and s["approved"]
    down = _bars([(100, 99), (99, 97.5)] + [(97.5, 97.5)] * 80)
    assert score_idea(_idea(False), down)["exit_reason"] == "stop"
    flat = _bars([(100, 100)] * 80)
    t = score_idea(_idea(True), flat)
    assert t["exit_reason"] == "time" and t["net_ret"] < 0  # costs only


def test_pending_when_exit_not_cached_and_summary():
    short = _bars([(100, 100)] * 3)
    nxt = _idea(True, exit_at="2026-10-14T09:35:00-04:00")
    assert score_idea(nxt, short) is None
    summ = summarize([{"model": "orb", "approved": True, "net_ret": 0.01},
                      {"model": "orb", "approved": False, "net_ret": -0.02}])
    assert summ["orb"]["approved"]["n"] == 1 and round(summ["all"]["rejected"]["avg_bps"]) == -200
