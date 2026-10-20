"""Autopilot: trailing stops, the two knobs, benching and the launcher override."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctbot import autopilot
from ctbot.config import RiskLimits
from ctbot.models import Decision, Position, Quote
from ctbot.risk.manager import AccountState, RiskManager

NOW = datetime(2026, 10, 21, 15, tzinfo=timezone.utc)


def _rm(max_loss=0.05, giveback=0.08):
    rm = RiskManager(RiskLimits())
    rm.max_loss_pct, rm.trail_giveback = max_loss, giveback
    return rm


def test_trailing_stop_ratchets_up_and_never_down():
    rm, st = _rm(), AccountState(10_000, 10_000, 10_000, 10_000, 0)
    rm.set_bracket(st, "KO", 100.0, Decision(True, 0.9, "", 1000, stop_loss_pct=0.04, take_profit_pct=0.10))
    b = st.brackets["KO"]
    assert b["stop"] == 96.0 and b["target"] is None and b["trail"] == 0.04  # Claude's stop, no fixed target
    assert rm.trail(st, "KO", 110.0) and b["stop"] == 105.6
    assert not rm.trail(st, "KO", 104.0) and b["stop"] == 105.6  # never moves down
    pos = Position("KO", 10, 100.0, NOW)
    assert rm.bracket_hit(st, pos, Quote("KO", 105.5, 105.6, 105.5)) == "trailing_stop"


def test_knobs_cap_claude_stop_and_trail():
    rm, st = _rm(max_loss=0.03, giveback=0.02), AccountState(10_000, 10_000, 10_000, 10_000, 0)
    rm.set_bracket(st, "KO", 100.0, Decision(True, 0.9, "", 1000, stop_loss_pct=0.09))
    assert st.brackets["KO"]["stop"] == 98.0 and st.brackets["KO"]["trail"] == 0.02


def test_model_stop_starts_tight_then_trails_by_the_giveback():
    rm, st = _rm(giveback=0.05), AccountState(10_000, 10_000, 10_000, 10_000, 0)
    rm.set_bracket(st, "AMD", 100.0, None, {"stop": 99.7, "target": 101.0, "exit_at": "2026-10-21T15:50:00-04:00",
                                            "model": "orb_sip"})
    b = st.brackets["AMD"]
    assert b["stop"] == 99.7 and b["target"] is None and b["trail"] == 0.05
    assert not rm.trail(st, "AMD", 104.0)  # 104 x 0.95 = 98.8 is still below the model's stop
    assert rm.trail(st, "AMD", 110.0) and b["stop"] == 104.5


def test_systematic_models_keep_their_own_exits():
    rm, st = _rm(), AccountState(10_000, 10_000, 10_000, 10_000, 0)
    rm.set_bracket(st, "QQQ", 100.0, None, {"stop": 60.0, "target": None, "exit_at": "2027-10-20T15:50:00-04:00",
                                            "model": "trend_core", "systematic": True})
    assert "trail" not in st.brackets["QQQ"] and st.brackets["QQQ"]["stop"] == 90.0  # global 10% cap, not 5%


def test_fixed_brackets_without_autopilot():
    rm, st = RiskManager(RiskLimits()), AccountState(10_000, 10_000, 10_000, 10_000, 0)
    rm.set_bracket(st, "KO", 100.0, Decision(True, 0.9, "", 1000, stop_loss_pct=0.04, take_profit_pct=0.10))
    assert st.brackets["KO"] == {"stop": 96.0, "target": 110.0}
    assert not rm.trail(st, "KO", 120.0)


def test_config_validation_and_roundtrip(tmp_path):
    p = tmp_path / "autopilot.json"
    assert autopilot.load(p) == autopilot.AutopilotConfig()  # off by default
    autopilot.save(p, autopilot.validate({"enabled": True, "max_loss_pct": 0.04, "giveback_pct": 0.1}))
    assert autopilot.load(p) == autopilot.AutopilotConfig(True, 0.04, 0.1)
    with pytest.raises(ValueError):
        autopilot.validate({"max_loss_pct": 0.5})


def _fill(model, side, px, ts, sym="AAA"):
    return {"symbol": sym, "side": side, "quantity": 1, "price": px, "ts": ts.isoformat(), "tag": model}


def test_losing_model_is_benched_and_gets_another_chance_later():
    fills = []
    for i in range(10):
        t = NOW - timedelta(days=5, hours=i)
        fills += [_fill("orb", "buy", 100, t), _fill("stop_loss", "sell", 99, t + timedelta(minutes=5))]
    v = autopilot.decide(["orb", "rsi2_etf", "ranker"], fills, NOW)
    assert v["orb"]["status"] == "benched" and v["orb"]["closed"] == 10
    assert v["rsi2_etf"]["status"] == "trading" and v["ranker"]["status"] == "benched"
    assert autopilot.decide(["orb"], fills, NOW + timedelta(days=40))["orb"]["status"] == "trading"


def test_autopilot_overrides_shadow_modes_in_the_pipeline():
    from ctbot.config import Settings
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import Components, apply_launcher
    from ctbot.strategies.registry import WeightedStrategy

    class S:
        def __init__(self, name):
            self.name = name

    class MD:
        def quotes(self, syms):
            return {}

    with tempfile.TemporaryDirectory() as d:
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(Path(d) / "l.json")})
        broker = PaperBroker(MD(), 10_000, Path(d) / "l.json", clock=lambda: NOW)
        strats = [WeightedStrategy(S("orb"), 1.0, mode="shadow"), WeightedStrategy(S("ranker"), 1.0, mode="shadow")]
        c = Components(s, MD(), [], strats, None, RiskManager(s.risk), broker, [])
        st = AccountState(10_000, 10_000, 10_000, 10_000, 0)
        apply_launcher(c, st, {}, NOW)
        assert [w.mode for w in c.strategies] == ["shadow", "shadow"] and c.risk.trail_giveback is None
        s.autopilot_path.write_text(json.dumps({"enabled": True, "max_loss_pct": 0.05, "giveback_pct": 0.07}))
        apply_launcher(c, st, {}, NOW)
        assert [(w.strategy.name, w.mode, w.standalone) for w in c.strategies] == [
            ("orb", "trade", True), ("ranker", "shadow", False)]
        assert c.risk.trail_giveback == 0.07 and c.risk.max_loss_pct == 0.05
        s.autopilot_path.write_text(json.dumps({"enabled": False}))
        apply_launcher(c, st, {}, NOW)
        assert [w.mode for w in c.strategies] == ["shadow", "shadow"] and c.risk.trail_giveback is None


def test_library_entries_need_the_backtest_to_admit_them():
    rep = {"entries": {
        "lib_rsi2_etfs": {"admitted": True, "checks": {}, "validation": {"cagr": 0.08, "sharpe": 0.9, "trades": 40}},
        "lib_nr7_stocks": {"admitted": False, "checks": {"fdr": False, "dev_sharpe_positive": True, "enough_trades": True},
                           "validation": {"cagr": 0.01, "sharpe": 0.1, "trades": 90}}}}
    v = autopilot.decide(["lib_rsi2_etfs", "lib_nr7_stocks", "lib_macd_etfs"], [], NOW, library=rep)
    assert v["lib_rsi2_etfs"]["status"] == "trading"
    assert v["lib_nr7_stocks"]["status"] == "benched" and "could be luck" in v["lib_nr7_stocks"]["reason"]
    assert v["lib_macd_etfs"]["status"] == "benched" and "not tested yet" in v["lib_macd_etfs"]["reason"]
