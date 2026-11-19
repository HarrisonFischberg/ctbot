"""The nightly stop-level setter: replay under each pair, move only on clear evidence."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ctbot import autopilot, levels
from ctbot.models import Bar

T0 = datetime(2026, 10, 1, 14, tzinfo=UTC)


def _bars(closes, start=T0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar("KO", start + timedelta(minutes=5 * i), prev, max(prev, c), min(prev, c), c, 1))
        prev = c
    return out


def _idea(ts=T0, stop_pct=0.10, days=1.0):
    return {"id": "x", "ts": ts.isoformat(), "symbol": "KO", "direction": "long",
            "decision": {"approve": True, "stop_loss_pct": stop_pct},
            "signals": [{"strategy": "momentum", "direction": "long", "horizon_days": days, "evidence": {}}]}


DAY = 24 * 12  # 5-minute bars in a calendar day


def test_replay_follows_set_bracket():
    dip = _bars([100, 95] + [106] * DAY)  # falls 5%, then recovers
    assert levels.replay(_idea(), dip, 0.04, 0.04) < -0.035  # a 4% stop sells the dip
    assert levels.replay(_idea(), dip, 0.08, 0.08) > 0.05   # an 8% stop rides it out
    run_up = _bars([100, 110, 104] + [104] * DAY)
    assert round(levels.replay(_idea(), run_up, 0.08, 0.04), 3) == 0.055  # trails 4% under 110
    assert levels.replay(_idea(stop_pct=6.0), dip, 0.12, 0.12) > 0.05  # an old row's 6.0 means 6%
    own = _idea()
    own["signals"][0] = {"strategy": "rsi2_etf", "direction": "long",
                         "evidence": {"exit_at": (T0 + timedelta(days=1)).isoformat(), "stop": 90, "exit_rule": "x"}}
    assert levels.replay(own, dip, 0.04, 0.04) is None  # keeps its own exit: these numbers don't touch it
    assert levels.replay(_idea(), _bars([100, 101]), 0.08, 0.08) is None  # horizon not reached


def _many(n_days, closes):
    ideas, bars = [], []
    for d in range(n_days):
        t = T0 + timedelta(days=d)
        ideas += [_idea(t + timedelta(minutes=5 * k)) for k in range(3)]
        bars += _bars([100] * 3 + closes(d), t)
    return ideas, bars


def test_moves_only_on_clear_evidence(tmp_path, monkeypatch):
    now = T0 + timedelta(days=40)
    ideas, bars = _many(30, lambda d: [95 - d % 2] + [106] * (DAY - 4))  # every day dips 5-6% and recovers
    ev = levels.evaluate(ideas, lambda s: bars, (0.04, 0.04), now)
    assert (ev["ideas"], ev["days"]) == (87, 29)  # the last day is not finished
    assert ev["best"][0] >= 0.08 and ev["t"] > 3
    assert levels.evaluate(ideas[:30], lambda s: bars, (0.04, 0.04), now)["best"] is None  # 10 days: too few

    s = SimpleNamespace(autopilot_path=tmp_path / "autopilot.json", ideas_path=tmp_path / "ideas.jsonl",
                        paper_ledger_path=tmp_path / "ledger.json")
    autopilot.save(s.autopilot_path, autopilot.validate({"enabled": True, "max_loss_pct": 0.07, "giveback_pct": 0.04}))
    monkeypatch.setattr(levels.cache, "load", lambda interval, sym: bars)
    monkeypatch.delenv("NOTIFY_URL", raising=False)
    first = levels.run(s, now)  # no ideas yet: the backtest's answer
    cfg = autopilot.load(s.autopilot_path)
    assert first["changed"] and (cfg.max_loss_pct, cfg.giveback_pct) == levels.PRIOR and cfg.enabled
    assert not levels.run(s, now)["changed"]  # nothing to replay: stays put
    s.ideas_path.write_text("\n".join(json.dumps(i) for i in ideas))
    flat = levels.run(s, now)
    assert not flat["changed"] or flat["t"] >= levels.MIN_T
    monkeypatch.setenv("LEVELS_AUTO", "false")
    autopilot.save(s.autopilot_path, autopilot.validate({"enabled": True, "max_loss_pct": 0.04, "giveback_pct": 0.04}))
    ev = levels.run(s, now)
    assert ev["target"][0] >= 0.08 and not ev["changed"] and autopilot.load(s.autopilot_path).max_loss_pct == 0.04
