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
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto", "MIN_AGREEING_STRATEGIES": "2"})
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
    rm = RiskManager(Settings.from_env({"PDT_RULE": "true"}).risk)
    sig = Signal("orb", "KO", Direction.LONG, 0.8, 0.25,
                 {"stop": 98, "target": 104, "exit_at": "2026-10-13T15:50:00-04:00"})
    from ctbot.models import Candidate

    cand = Candidate("KO", Direction.LONG, 0.5, (sig,))
    busy = AccountState(250, 250, 250, 250, 0, day_trade_dates=[date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)])
    v = rm.check_entry(cand, Decision(True, 0.9, "", 50), busy, Quote("KO", 100, 100, 100), 1e9, today=DAY)
    assert "pdt_budget_used" in v.reasons
    free = AccountState(250, 250, 250, 250, 0)
    assert rm.check_entry(cand, Decision(True, 0.9, "", 50), free, Quote("KO", 100, 100, 100), 1e9, today=DAY).allowed
    # default (no PDT rule): the same busy account may still open a day trade
    rm = RiskManager(Settings.from_env({"MAX_DAY_TRADES_5D": "3"}).risk)
    assert rm.check_entry(cand, Decision(True, 0.9, "", 50), busy, Quote("KO", 100, 100, 100), 1e9, today=DAY).allowed


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
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "manual", "MAX_CLAUDE_REVIEWS_PER_DAY": "2"})
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
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "manual", "MAX_REVIEWS_PER_MODEL_PER_DAY": "1",
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
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto"})
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
        settings = Settings.from_env({"ENTRY_MODE": "auto", "REVIEW_SHADOW_IDEAS": "true",
                                      "PAPER_LEDGER_PATH": str(Path(d) / "l.json")})
        c = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True, mode="shadow")],
                       engine, RiskManager(settings.risk), broker, ["KO"], recorder=ideas.append)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
    assert engine.calls == 1 and broker._fills == []
    assert [(i["symbol"], i["status"], i["risk"]["allowed"]) for i in ideas] == [("KO", "shadow", True)]


def test_shadow_ideas_are_not_reviewed_by_default_and_spend_cap_stops_reviews():
    import json

    exit_at = datetime(2026, 10, 13, 19, 50, tzinfo=timezone.utc).isoformat()
    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        md = _MD(100.0)
        settings = Settings.from_env({"ENTRY_MODE": "auto", "PAPER_LEDGER_PATH": str(Path(d) / "l.json")})
        engine, ideas = _Engine(), []
        broker = PaperBroker(md, 250.0, Path(d) / "l.json", clock=lambda: now)
        c = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True, mode="shadow")],
                       engine, RiskManager(settings.risk), broker, ["KO"], recorder=ideas.append)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
        assert engine.calls == 0 and [i["status"] for i in ideas] == ["shadow_unreviewed"]
        # a trading model is reviewed until today's logged spend reaches the cap
        settings.llm_usage_path.write_text(json.dumps({"day": "2026-10-13", "usd": 5.0}) + "\n")
        engine2, ideas2 = _Engine(), []
        c2 = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True, mode="trade")],
                        engine2, RiskManager(settings.risk), broker, ["KO"], recorder=ideas2.append)
        run_cycle(c2, AccountState(250, 250, 250, 250, 0), now)
        assert engine2.calls == 0 and [i["status"] for i in ideas2] == ["review_budget_used"]


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


class _SpyMD(_MD):
    """SPY closed at 100 yesterday and is at `at_10` at 10:00 ET, flat after."""

    def __init__(self, at_10):
        super().__init__(at_10)
        self.at_10 = at_10

    def today_bars(self, symbols):
        return {"SPY": [Bar("SPY", OPEN + timedelta(minutes=5 * i), self.at_10, self.at_10, self.at_10, self.at_10, 1e6)
                        for i in range(78)]}

    def daily_bars(self, symbol, n):
        return [Bar(symbol, datetime(2026, 10, 12, 20, tzinfo=timezone.utc), 100, 100, 100, 100.0, 1e9)]


def test_intraday_momentum_buys_spy_late_only_after_an_up_morning():
    from ctbot.strategies.intraday import IntradayMomentum

    at = datetime(2026, 10, 13, 19, 35, tzinfo=timezone.utc)  # 15:35 ET
    ctx = lambda md, now=at, held=frozenset(): StrategyContext(md, ("SPY",), (), DAY, held, now)  # noqa: E731
    (sig,) = IntradayMomentum().generate(ctx(_SpyMD(100.5)))
    ev = sig.evidence
    assert sig.symbol == "SPY" and ev["systematic"] and ev["size_pct"] == 0.25 and ev["first_half_hour"] == 0.005
    assert ev["exit_at"].startswith("2026-10-13T15:55") and ev["stop"] == 99.495
    assert IntradayMomentum().generate(ctx(_SpyMD(99.5))) == []  # down morning
    assert IntradayMomentum().generate(ctx(_SpyMD(100.5), now=at - timedelta(minutes=10))) == []  # before 15:30
    assert IntradayMomentum().generate(ctx(_SpyMD(100.5), held=frozenset({"SPY"}))) == []


def test_intraday_momentum_trades_without_review_and_exits_by_time():
    from ctbot.strategies.intraday import IntradayMomentum

    at = datetime(2026, 10, 13, 19, 35, tzinfo=timezone.utc)
    md, engine = _SpyMD(100.5), _Engine()
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 10_000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: at)
        settings = Settings.from_env({"PAPER_LEDGER_PATH": str(broker.ledger_path), "ENTRY_MODE": "auto",
                                      "MAX_TRADING_CAPITAL": "10000"})
        rm = RiskManager(settings.risk)
        rm.trail_giveback = 0.08  # autopilot on: a systematic entry still keeps its own exit
        c = Components(settings, md, [], [WeightedStrategy(IntradayMomentum(), 1.0, standalone=True)],
                       engine, rm, broker, ["SPY"])
        state = AccountState(10_000, 10_000, 10_000, 10_000, 0)
        run_cycle(c, state, at)
        assert engine.calls == 0 and [f.tag for f in broker._fills] == ["intraday_momentum"]
        assert 2_000 < broker.positions()[0].quantity * 100.5 <= 2_500.01  # its own 25% size
        assert "trail" not in state.brackets["SPY"] and state.brackets["SPY"]["exit_at"].startswith("2026-10-13T15:55")
        run_cycle(c, state, datetime(2026, 10, 13, 19, 56, tzinfo=timezone.utc))  # 15:56 ET
        assert broker.positions() == [] and broker._fills[-1].tag == "time_exit"


def test_intraday_exit_before_early_close(monkeypatch):
    """B1: on a 13:00 ET early close (Fri 2026-11-27) same-day exits land at 12:50 ET, not 15:50."""
    from ctbot import market_hours
    from ctbot.config import RiskLimits
    from ctbot.strategies.intraday import OpeningRangeBreakout
    from ctbot.strategies.orb_sip import OrbStocksInPlay

    half = date(2026, 11, 27)
    t0 = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)  # 9:30 EST
    latest = datetime(2026, 11, 27, 12, 50, tzinfo=market_hours.ET)
    assert market_hours.exit_by(half) == latest
    assert market_hours.exit_by(DAY).isoformat() == "2026-10-13T15:50:00-04:00"  # normal day unchanged

    rng = [Bar("KO", t0 + timedelta(minutes=5 * i), 100, 100.2, 99.8, 100, 1e5) for i in range(6)]
    breakout = rng + [Bar("KO", t0 + timedelta(minutes=30), 100, 101.2, 100, 101, 1e5)]

    class _Bars(_MD):
        def today_bars(self, symbols):
            return {"KO": breakout}

    monkeypatch.setattr(OpeningRangeBreakout, "_avg_first30", lambda self, sym, d: 3e5)
    now = t0 + timedelta(minutes=40)  # 10:10 EST
    (orb,) = OpeningRangeBreakout().generate(StrategyContext(_Bars(), ("KO",), (), half, frozenset(), now))
    assert datetime.fromisoformat(orb.evidence["exit_at"]) <= latest

    class _Scan:
        def today(self):
            return [{"symbol": "KO", "rvol_ratio": 0.2, "atr14": 2.0, "last": 100}]

    green = [Bar("KO", t0, 100, 101, 99.8, 100.8, 1e5), Bar("KO", t0 + timedelta(minutes=5), 100.8, 101.2, 100.7, 101.1, 1e5)]

    class _Green(_MD):
        def today_bars(self, symbols):
            return {"KO": green}

    (sip,) = OrbStocksInPlay(scanner=_Scan(), confirm="touch").generate(
        StrategyContext(_Green(), ("KO",), (), half, frozenset(), t0 + timedelta(minutes=11)))
    assert datetime.fromisoformat(sip.evidence["exit_at"]) == latest

    # no vwap_revert entry so late that its exit has already passed (12:55 EST on the half day)
    late = datetime(2026, 11, 27, 17, 55, tzinfo=timezone.utc)
    assert VwapReversion().generate(StrategyContext(_MD(), ("KO",), (), half, frozenset(), late)) == []

    # a winner swinging from Wednesday 11-25 rolls past the Thanksgiving holiday to the half day,
    # where its new exit is 12:50 ET
    rm = RiskManager(RiskLimits(swing_min_profit_pct=0.01, swing_max_days=1))
    st = AccountState(250, 250, 250, 250, 1, brackets={"KO": {"stop": 98.5, "target": None,
                      "exit_at": "2026-11-25T15:50:00-05:00", "model": "orb"}})
    assert rm.maybe_swing(st, Position("KO", 1.0, 100.0), Quote("KO", 102, 102, 102),
                          datetime(2026, 11, 25, 20, 40, tzinfo=timezone.utc))
    assert datetime.fromisoformat(st.brackets["KO"]["exit_at"]) == latest



def test_spend_cap_counts_every_book():
    """B2: MAX_CLAUDE_USD_PER_DAY caps the main book and every side-by-side book in total; a
    book's own (here larger) cap only limits that book."""
    import json

    from ctbot import llm_costs

    exit_at = datetime(2026, 10, 13, 19, 50, tzinfo=timezone.utc).isoformat()
    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)

    def usage(path, usd):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": "2026-10-13", "usd": usd}) + "\n")

    def reviewed(settings, total_spend=None):
        md, engine = _MD(100.0), _Engine()
        broker = PaperBroker(md, 250.0, settings.paper_ledger_path, clock=lambda: now)
        c = Components(settings, md, [], [WeightedStrategy(_Fixed(exit_at), 1.0, standalone=True)],
                       engine, RiskManager(settings.risk), broker, ["KO"], total_spend=total_spend)
        run_cycle(c, AccountState(250, 250, 250, 250, 0), now)
        return engine.calls == 1

    with tempfile.TemporaryDirectory() as d:
        main = Settings.from_env({"ENTRY_MODE": "auto", "PAPER_LEDGER_PATH": str(Path(d) / "paper_ledger.json"),
                                  "MAX_CLAUDE_USD_PER_DAY": "1.0"})
        book = Settings.from_env({"ENTRY_MODE": "auto", "MAX_CLAUDE_USD_PER_DAY": "3.0",
                                  "PAPER_LEDGER_PATH": str(Path(d) / "variants" / "sonnet" / "paper_ledger.json")})
        total = (main.llm_usage_path, main.max_claude_usd_per_day)
        usage(main.llm_usage_path, 0.2)
        usage(book.llm_usage_path, 0.3)
        assert llm_costs.spent_all_books(main.llm_usage_path, date(2026, 10, 13)) == 0.5
        assert reviewed(book, total)  # $0.50 of $1 in total: still reviewing
        usage(book.llm_usage_path, 0.6)  # the book alone is still well under its own $3 cap
        usage(main.llm_usage_path, 0.4)
        assert llm_costs.spent_all_books(main.llm_usage_path, date(2026, 10, 13)) == 1.0
        assert not reviewed(book, total)
        assert not reviewed(main)  # the main book counts the books' spend too
