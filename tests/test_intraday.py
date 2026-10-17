"""Live intraday paper models end to end: signal, Claude once per day, plan bracket, time exit."""

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ctbot.config import Settings
from ctbot.models import Bar, Decision, Direction, Position, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, run_cycle
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.intraday import OvernightHold, VwapReversion
from ctbot.strategies.registry import WeightedStrategy

DAY = date(2026, 10, 13)
OPEN = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)  # 9:30 ET


class _MD:
    """Flat at 100 all morning, then a dip to 98.5 at 11:00 ET; KO only."""

    def __init__(self, px=100.0):
        self.px = px

    def today_bars(self, symbols):
        bars = []
        for i in range(78):
            c = 98.5 if 18 <= i <= 20 else 100.0
            bars.append(Bar("KO", OPEN + timedelta(minutes=5 * i), 100, 100.2, min(c, 99.8), c, 1e5))
        return {"KO": bars}

    def daily_bars(self, symbol, n):
        return [Bar(symbol, datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=i), 1, 1, 1, 90 + i * 0.1, 1e7)
                for i in range(n)]  # rising: in an uptrend

    def quotes(self, symbols):
        return {s: Quote(s, self.px, self.px, self.px) for s in symbols}

    def is_market_open(self):
        return True


def _ctx(now, held=frozenset(), day=DAY):
    return StrategyContext(_MD(), ("KO",), (), day, held, now)


def test_vwap_signal_carries_exit_plan_and_respects_hours():
    at = OPEN + timedelta(minutes=5 * 19)  # 11:05 ET, dip bar completed
    sigs = VwapReversion().generate(_ctx(at))
    assert len(sigs) == 1 and sigs[0].strategy == "vwap_revert"
    ev = sigs[0].evidence
    assert ev["stop"] < ev["ref_price"] < ev["target"] and ev["exit_at"].startswith("2026-10-13T15:50")
    assert VwapReversion().generate(_ctx(OPEN + timedelta(minutes=30))) == []  # before 10:30
    assert VwapReversion().generate(_ctx(at, held=frozenset({"KO"}))) == []


def test_overnight_exit_is_next_business_day():
    fri = datetime(2026, 10, 16, 19, 35, tzinfo=timezone.utc)  # Fri 15:35 ET
    sigs = OvernightHold().generate(_ctx(fri, day=date(2026, 10, 16)))
    assert sigs and sigs[0].evidence["exit_at"].startswith("2026-10-19T09:35")  # Monday


class _Engine:
    def __init__(self):
        self.calls = 0

    def evaluate(self, cand, account):
        self.calls += 1
        return Decision(True, 0.9, "ok", 50.0, stop_loss_pct=0.10)


class _Fixed:
    name, event_kinds = "vwap_revert", frozenset()

    def __init__(self, exit_at):
        self.exit_at = exit_at

    def generate(self, ctx):
        return [Signal("vwap_revert", "KO", Direction.LONG, 0.8, 0.2,
                       {"ref_price": 100.0, "stop": 98.5, "target": 101.0, "exit_at": self.exit_at})]


class _MeanRevExit:
    name, event_kinds = "mean_reversion", frozenset()

    def generate(self, ctx):
        return [Signal("mean_reversion", s, Direction.EXIT, 0.6, 1.0) for s in ctx.held_symbols]


def test_end_to_end_buy_once_tagged_then_time_exit():
    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    exit_at = datetime(2026, 10, 13, 19, 50, tzinfo=timezone.utc).isoformat()
    md, engine = _MD(100.0), _Engine()
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: now)
        settings = Settings.from_env({"ENTRY_MODE": "auto", "MIN_AGREEING_STRATEGIES": "2"})
        c = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True),
                                          WeightedStrategy(_MeanRevExit(), 0.75)],
                       engine, RiskManager(settings.risk), broker, ["KO"])
        state = AccountState(250, 250, 250, 250, 0)
        run_cycle(c, state, now)
        assert [f.tag for f in broker._fills] == ["vwap_revert"]
        b = state.brackets["KO"]
        # model plan wins over Claude's 10% stop; daily mean_reversion EXIT is ignored
        assert b["stop"] == 98.5 and b["target"] == 101.0 and b["model"] == "vwap_revert"
        broker._positions["KO"].opened_at = now
        run_cycle(c, state, now + timedelta(minutes=5))
        assert engine.calls == 1 and len(broker.positions()) == 1  # reviewed once; still held
        run_cycle(c, state, datetime(2026, 10, 13, 19, 51, tzinfo=timezone.utc))  # 15:51 ET
        assert broker.positions() == [] and broker._fills[-1].tag == "time_exit"
        assert state.day_trade_dates == [date(2026, 10, 13)]


def test_day_trade_entry_refused_when_pdt_budget_used():
    rm = RiskManager(Settings.from_env({}).risk)
    sig = Signal("orb", "KO", Direction.LONG, 0.8, 0.25,
                 {"stop": 98, "target": 104, "exit_at": "2026-10-13T15:50:00-04:00"})
    from ctbot.models import Candidate

    cand = Candidate("KO", Direction.LONG, 0.5, (sig,))
    busy = AccountState(250, 250, 250, 250, 0, day_trade_dates=[date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)])
    v = rm.check_entry(cand, Decision(True, 0.9, "", 50), busy, Quote("KO", 100, 100, 100), 1e9, today=DAY)
    assert "pdt_budget_used" in v.reasons
    free = AccountState(250, 250, 250, 250, 0)
    assert rm.check_entry(cand, Decision(True, 0.9, "", 50), free, Quote("KO", 100, 100, 100), 1e9, today=DAY).allowed


def test_fill_tag_survives_ledger_reload():
    from ctbot.models import OrderRequest, Side

    with tempfile.TemporaryDirectory() as d:
        b = PaperBroker(_MD(), 250.0, Path(d) / "l.json", slippage_bps=0)
        b.submit(OrderRequest("KO", Side.BUY, 50.0, candidate_ref="orb"))
        assert PaperBroker(_MD(), 250.0, Path(d) / "l.json")._fills[0].tag == "orb"


def test_winner_swings_to_next_close_with_raised_stop_and_loser_exits():
    from ctbot.config import RiskLimits

    rm = RiskManager(RiskLimits(swing_min_profit_pct=0.01, swing_max_days=1))
    pos = Position("KO", 1.0, 100.0)
    exit_at = "2026-10-13T15:50:00-04:00"
    st = AccountState(250, 250, 250, 250, 1, brackets={"KO": {"stop": 98.5, "target": None, "exit_at": exit_at, "model": "orb"}})
    at = datetime(2026, 10, 13, 19, 40, tzinfo=timezone.utc)  # 15:40 ET
    assert not rm.maybe_swing(st, pos, Quote("KO", 100.5, 100.5, 100.5), at)  # only +0.5%
    assert rm.maybe_swing(st, pos, Quote("KO", 101.5, 101.5, 101.5), at)
    b = st.brackets["KO"]
    assert b["exit_at"].startswith("2026-10-14T15:50") and b["stop"] == 100.2 and b["swings"] == 1
    next_close = datetime(2026, 10, 14, 19, 40, tzinfo=timezone.utc)
    assert not rm.maybe_swing(st, pos, Quote("KO", 103, 103, 103), next_close)  # max 1 swing
    assert rm.bracket_hit(st, pos, Quote("KO", 103, 103, 103), datetime(2026, 10, 14, 19, 51, tzinfo=timezone.utc)) == "time_exit"
    assert rm.bracket_hit(st, pos, Quote("KO", 100.1, 100.1, 100.1), next_close) == "stop_loss"  # gain locked


def test_overnight_winner_extends_to_same_day_close():
    from ctbot.config import RiskLimits

    rm = RiskManager(RiskLimits())
    st = AccountState(250, 250, 250, 250, 1, brackets={"XLE": {"stop": 65, "target": None,
                      "exit_at": "2026-10-14T09:35:00-04:00", "model": "overnight"}})
    assert rm.maybe_swing(st, Position("XLE", 1, 67.0), Quote("XLE", 68, 68, 68),
                          datetime(2026, 10, 14, 13, 31, tzinfo=timezone.utc))  # 9:31 ET
    assert st.brackets["XLE"]["exit_at"].startswith("2026-10-14T15:50")


def test_model_paused_after_losing_streak_and_reset_next_day():
    from ctbot.config import RiskLimits

    rm = RiskManager(RiskLimits(model_max_losing_streak=2))
    st = AccountState(250, 250, 250, 250, 0, day_start_date=DAY)
    for px in (99.0, 98.0):
        st.brackets["KO"] = {"stop": 90, "target": None, "model": "orb"}
        rm.record_exit_fill(st, Position("KO", 1, 100.0), DAY, px)
    assert rm.model_paused(st, "orb") and not rm.model_paused(st, "vwap_revert")
    st.brackets["KO"] = {"stop": 90, "target": None, "model": "vwap_revert"}
    rm.record_exit_fill(st, Position("KO", 1, 100.0), DAY, 101.0)  # a win resets that model
    assert st.model_losing_streak["vwap_revert"] == 0
    rm.refresh(st, date(2026, 10, 14))
    assert not rm.model_paused(st, "orb")


def test_daily_claude_review_budget():
    syms = ["KO", "PEP", "XLE"]

    class _Many:
        name, event_kinds = "vwap_revert", frozenset()

        def generate(self, ctx):
            return [Signal("vwap_revert", s, Direction.LONG, 0.8, 0.2,
                           {"stop": 98, "target": 101, "exit_at": "2026-10-13T15:50:00-04:00"}) for s in syms]

    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    engine, ideas = _Engine(), []
    with tempfile.TemporaryDirectory() as d:
        md = _MD(100.0)
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", clock=lambda: now)
        settings = Settings.from_env({"ENTRY_MODE": "manual", "MAX_CLAUDE_REVIEWS_PER_DAY": "2"})
        c = Components(settings, md, [], [WeightedStrategy(_Many(), 1.0, standalone=True)], engine,
                       RiskManager(settings.risk), broker, syms, recorder=ideas.append)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
    assert engine.calls == 2 and [i["status"] for i in ideas].count("review_budget_used") == 1


def test_per_model_review_cap():
    class _Model:
        event_kinds = frozenset()

        def __init__(self, name, syms):
            self.name, self.syms = name, syms

        def generate(self, ctx):
            return [Signal(self.name, s, Direction.LONG, 0.8, 0.2,
                           {"stop": 98, "target": 101, "exit_at": "2026-10-13T15:50:00-04:00"}) for s in self.syms]

    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    engine, ideas = _Engine(), []
    with tempfile.TemporaryDirectory() as d:
        md = _MD(100.0)
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", clock=lambda: now)
        settings = Settings.from_env({"ENTRY_MODE": "manual", "MAX_REVIEWS_PER_MODEL_PER_DAY": "1",
                                      "MAX_CANDIDATES_PER_CYCLE": "10"})
        c = Components(settings, md, [], [WeightedStrategy(_Model("vwap_revert", ["KO", "PEP"]), 1.0, standalone=True),
                                          WeightedStrategy(_Model("orb", ["XLE"]), 1.0, standalone=True)],
                       engine, RiskManager(settings.risk), broker, ["KO", "PEP", "XLE"], recorder=ideas.append)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
    reviewed = sorted(i["symbol"] for i in ideas if i["decision"])
    assert engine.calls == 2 and "XLE" in reviewed  # orb still reviewed after vwap hit its cap


def test_model_exit_only_closes_its_own_positions():
    class _Exit:
        name, event_kinds = "rsi2_etf", frozenset()

        def generate(self, ctx):
            return [Signal("rsi2_etf", s, Direction.EXIT, 1.0, 0) for s in ctx.held_symbols]

    now = datetime(2026, 10, 13, 19, 35, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        md = _MD(100.0)
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: now)
        broker._positions["KO"] = Position("KO", 0.5, 100.0, now)
        broker._positions["PEP"] = Position("PEP", 0.5, 100.0, now)
        settings = Settings.from_env({"ENTRY_MODE": "auto"})
        c = Components(settings, md, [], [WeightedStrategy(_Exit(), 1.0, standalone=True)], _Engine(),
                       RiskManager(settings.risk), broker, ["KO", "PEP"])
        st = AccountState(250, 200, 250, 250, 2, brackets={
            "KO": {"stop": 50, "target": None, "exit_at": "2026-10-30T15:50:00-04:00", "model": "rsi2_etf"},
            "PEP": {"stop": 50, "target": None, "exit_at": "2026-10-30T15:50:00-04:00", "model": "orb"}})
        run_cycle(c, st, now)
    assert [p.symbol for p in broker.positions()] == ["PEP"]


def test_shadow_model_is_reviewed_and_recorded_but_not_traded():
    exit_at = datetime(2026, 10, 13, 19, 50, tzinfo=timezone.utc).isoformat()
    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    engine, ideas = _Engine(), []
    with tempfile.TemporaryDirectory() as d:
        md = _MD(100.0)
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", clock=lambda: now)
        settings = Settings.from_env({"ENTRY_MODE": "auto"})
        c = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True, mode="shadow")],
                       engine, RiskManager(settings.risk), broker, ["KO"], recorder=ideas.append)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
    assert engine.calls == 1 and broker._fills == []
    assert [(i["symbol"], i["status"], i["risk"]["allowed"]) for i in ideas] == [("KO", "shadow", True)]


def test_registry_rejects_bad_mode():
    import pytest

    from ctbot.strategies.registry import build_strategies

    with pytest.raises(ValueError):
        build_strategies({"orb": {"enabled": True, "mode": "live"}})


class _FlakyMD(_MD):
    """today_bars raises `fails` times (a data outage at the open), then serves bars."""

    def __init__(self, fails):
        super().__init__()
        self.fails, self.calls = fails, 0

    def today_bars(self, symbols):
        self.calls += 1
        if self.calls <= self.fails:
            raise ConnectionError("robinhood timed out")
        return super().today_bars(symbols)


def test_bar_load_retries_a_transient_outage(monkeypatch):
    import ctbot.strategies.intraday as intraday

    waits = []
    monkeypatch.setattr(intraday._time, "sleep", waits.append)
    at = OPEN + timedelta(minutes=5 * 19)  # 11:05 ET, dip bar completed
    md = _FlakyMD(fails=2)
    sigs = VwapReversion().generate(StrategyContext(md, ("KO",), (), DAY, frozenset(), at))
    assert len(sigs) == 1 and md.calls == 3 and waits == list(intraday.BAR_RETRY_WAITS_S)


def test_bar_load_gives_up_after_bounded_attempts(monkeypatch):
    import ctbot.strategies.intraday as intraday

    monkeypatch.setattr(intraday._time, "sleep", lambda s: None)
    at = OPEN + timedelta(minutes=5 * 19)
    md = _FlakyMD(fails=99)
    assert VwapReversion().generate(StrategyContext(md, ("KO",), (), DAY, frozenset(), at)) == []
    assert md.calls == len(intraday.BAR_RETRY_WAITS_S) + 1  # skips this cycle, next cycle tries again
