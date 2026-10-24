"""Trading-skill rules: partial profits at 1R, the VWAP gate and the bear-regime gate (all off by default)."""

import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctbot.config import RiskLimits, Settings
from ctbot.models import Candidate, Decision, Direction, Position, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, manage_positions
from ctbot.risk.manager import AccountState, RiskManager

NOW = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)  # Tue 11:00 ET
OK = Decision(approve=True, confidence=0.8, rationale="", suggested_notional=100.0)


def _state(**kw):
    return AccountState(**{**dict(equity=250, cash=250, peak_equity=250, day_start_equity=250,
                                  open_positions=0), **kw})


def _intraday(systematic=False):
    ev = {"exit_at": (NOW + timedelta(hours=4)).isoformat(), "stop": 59.0, "ref_price": 60.0}
    if systematic:
        ev["systematic"] = True
    return Candidate("KO", Direction.LONG, 0.4, (Signal("orb_sip", "KO", Direction.LONG, 0.8, 0, ev),))


def _check(limits, cand, last=60.01, **kw):
    q = Quote("KO", 60.0, 60.02, last, ts=NOW)
    return RiskManager(limits).check_entry(cand, OK, _state(), q, 1e9, today=NOW.date(), now=NOW, **kw)


def test_gates_are_off_by_default():
    v = _check(RiskLimits(), _intraday(), vwap=61.0, market_regime="bear")
    assert v.allowed


def test_vwap_gate_refuses_same_day_buy_at_or_below_vwap():
    L = replace(RiskLimits(), vwap_entry_gate=True)
    assert "below_vwap" in _check(L, _intraday(), vwap=61.0).reasons
    assert _check(L, _intraday(), vwap=59.0).allowed
    assert _check(L, _intraday(), vwap=None).allowed  # no bars: no gate


def test_regime_gate_blocks_reviewed_entries_only():
    L = replace(RiskLimits(), regime_gate=True)
    assert "bear_regime" in _check(L, _intraday(), market_regime="bear").reasons
    assert _check(L, _intraday(), market_regime="sideways").allowed
    assert _check(L, _intraday(systematic=True), market_regime="bear").allowed


def test_partial_due_once_at_1r():
    rm = RiskManager(replace(RiskLimits(), partial_profit_r=1.0))
    s = _state()
    rm.set_bracket(s, "KO", 60.0, None, {"stop": 58.0, "exit_at": NOW.isoformat()})
    pos = Position("KO", 2.0, 60.0, NOW)
    assert s.brackets["KO"]["r0"] == 2.0
    assert rm.partial_due(s, pos, Quote("KO", 61.9, 62.0, 61.9)) == 0.0
    assert rm.partial_due(s, pos, Quote("KO", 62.0, 62.1, 62.0)) == 1.0
    rm.partial_taken(s, "KO", 60.0)
    assert s.brackets["KO"]["stop"] == 60.0
    assert rm.partial_due(s, pos, Quote("KO", 63.0, 63.1, 63.0)) == 0.0
    assert RiskManager(RiskLimits()).partial_due(s, pos, Quote("KO", 70.0, 70.1, 70.0)) == 0.0


class _MD:
    def __init__(self):
        self.px = 50.0

    def daily_bars(self, symbol, lookback_days):
        return []

    def quotes(self, symbols):
        return {s: Quote(s, self.px, self.px, self.px, ts=NOW) for s in symbols}

    def is_market_open(self):
        return True


def test_manage_positions_sells_half_then_keeps_the_rest_at_break_even():
    with tempfile.TemporaryDirectory() as d:
        md = _MD()
        broker = PaperBroker(md, 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW,
                             instant_settlement=True)
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "PARTIAL_PROFIT_R": "1",
                                      "RESPECT_MARKET_HOURS": "false"})
        c = Components(settings=settings, market_data=md, providers=[], strategies=[], engine=None,
                       risk=RiskManager(settings.risk), executor=broker, universe=[])
        from ctbot.models import OrderRequest, Side

        broker.submit(OrderRequest("KO", Side.BUY, 100.0, 50.0))
        state = _state(cash=broker.cash(), open_positions=1)
        c.risk.set_bracket(state, "KO", 50.0, None, {"stop": 48.0, "exit_at": (NOW + timedelta(hours=4)).isoformat()})
        md.px = 52.0  # +1R
        manage_positions(c, state, NOW)
        pos = broker.positions()[0]
        assert abs(pos.quantity - 1.0) < 1e-6  # 2 shares bought, 1 sold
        assert state.brackets["KO"]["partial_done"] and state.brackets["KO"]["stop"] == 50.0
        md.px = 49.9  # back through break-even: the rest goes
        manage_positions(c, state, NOW)
        assert broker.positions() == [] and "KO" not in state.brackets
