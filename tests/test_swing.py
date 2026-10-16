"""Multi-day ETF models: RSI(2) entries/own exits and turn-of-month calendar."""

from datetime import date, datetime, timedelta, timezone

from ctbot.models import Bar, Quote
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.swing import Rsi2Etf, TurnOfMonth, rsi

NOW = datetime(2026, 10, 14, 19, 35, tzinfo=timezone.utc)  # Wed 15:35 ET


class _MD:
    def __init__(self, closes, last):
        self.closes, self.last = closes, last

    def daily_bars(self, sym, n):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [Bar(sym, t0 + timedelta(days=i), c, c, c, c, 1e7) for i, c in enumerate(self.closes[-n:])]

    def quotes(self, syms):
        return {s: Quote(s, self.last, self.last, self.last) for s in syms}


def _ctx(md, now=NOW, held=frozenset()):
    return StrategyContext(md, ("SPY", "KO"), (), now.date(), held, now)


def test_rsi_helper():
    assert rsi([10, 9, 8]) == 0.0 and rsi([8, 9, 10]) == 100.0


def test_rsi2_buys_dip_in_uptrend_and_exits_own_position_above_sma5():
    up = [100 + i * 0.1 for i in range(200)]          # long uptrend, last close 119.9
    dip = Rsi2Etf().generate(_ctx(_MD(up[:-2] + [119.0, 118.0], 117.0)))
    assert [(s.symbol, s.direction.value) for s in dip] == [("SPY", "long")]
    assert dip[0].evidence["exit_at"].startswith("2026-10-28T15:50")  # 10 business days
    held = Rsi2Etf().generate(_ctx(_MD(up, 125.0), held=frozenset({"SPY"})))
    assert [(s.symbol, s.direction.value) for s in held] == [("SPY", "exit")]
    assert Rsi2Etf().generate(_ctx(_MD(up[:-2] + [119.0, 118.0], 117.0), now=NOW - timedelta(hours=2))) == []


def test_turn_of_month_calendar():
    assert TurnOfMonth.is_entry_day(date(2026, 10, 29))      # Oct 30 (Fri) is the last trading day
    assert not TurnOfMonth.is_entry_day(date(2026, 10, 30))
    assert TurnOfMonth.exit_day(date(2026, 10, 29)) == date(2026, 11, 4)  # Nov 2, 3, 4 -> 3rd
    at = datetime(2026, 10, 29, 19, 35, tzinfo=timezone.utc)
    sigs = TurnOfMonth().generate(StrategyContext(_MD([1] * 5, 600.0), ("SPY", "QQQ"), (), at.date(), frozenset(), at))
    assert {s.symbol for s in sigs} == {"SPY", "QQQ"} and sigs[0].evidence["exit_at"].startswith("2026-11-04T15:50")


def test_trend_core_buys_without_review_at_requested_size():
    import tempfile
    from pathlib import Path

    from ctbot.config import Settings
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import Components, run_cycle
    from ctbot.risk.manager import AccountState, RiskManager
    from ctbot.strategies.registry import WeightedStrategy
    from ctbot.strategies.swing import TrendCore

    class _Engine:
        calls = 0

        def evaluate(self, *a):
            _Engine.calls += 1
            raise AssertionError("trend core must not call Claude")

    up = [400 + i for i in range(200)]
    md = _MD(up, 650.0)
    md.daily_bars = lambda sym, n: [Bar(sym, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i), c, c, c, c, 1e9)
                                    for i, c in enumerate(up[-n:])]
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        s = Settings.from_env({"ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "STARTING_CAPITAL": "5000"})
        c = Components(s, md, [], [WeightedStrategy(TrendCore(), 1.0, standalone=True)], _Engine(),
                       RiskManager(s.risk), broker, ["QQQ"])
        st = AccountState(5000, 5000, 5000, 5000, 0)
        run_cycle(c, st, NOW)
        assert [(f.symbol, round(f.quantity * f.price), f.tag) for f in broker._fills] == [("QQQ", 2500, "trend_core")]
        md.last = 500.0  # falls below the 200-day average -> its own exit
        st.cash = broker.cash()
        run_cycle(c, st, NOW + timedelta(minutes=5))
        assert broker.positions() == []


def test_core_tops_up_after_slipping_below_target():
    import tempfile
    from pathlib import Path

    from ctbot.config import Settings
    from ctbot.models import Position
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import Components, run_cycle
    from ctbot.risk.manager import AccountState, RiskManager
    from ctbot.strategies.registry import WeightedStrategy
    from ctbot.strategies.swing import TrendCore

    up = [400 + i for i in range(200)]
    md = _MD(up, 650.0)
    md.daily_bars = lambda sym, n: [Bar(sym, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i), c, c, c, c, 1e9)
                                    for i, c in enumerate(up[-n:])]
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        broker._cash = 2400.0
        broker._positions["QQQ"] = Position("QQQ", 4.0, 650.0)  # $2,600 of a $4,000 target
        s = Settings.from_env({"ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "MAX_CORE_PCT": "0.8"})
        c = Components(s, md, [], [WeightedStrategy(TrendCore(size_pct=0.8), 1.0, standalone=True)], None,
                       RiskManager(s.risk), broker, ["QQQ"])
        st = AccountState(5000, 2400, 5000, 5000, 1, brackets={"QQQ": {"stop": 500, "target": None,
                          "exit_at": "2027-10-01T15:50:00-04:00", "model": "trend_core"}})
        run_cycle(c, st, NOW)
        assert round(broker.positions()[0].quantity * 650) == 4000


def test_scanner_parse_ranks_by_relative_volume():
    from ctbot.market_data.scanner import parse_scan

    payload = {"data": {"result": {"results": [
        {"ticker": "aaa", "columns": {"RVOL": "0.05", "ATR14": "2.0", "Last": "50"}},
        {"ticker": "BBB", "columns": {"RVOL": "0.20", "ATR14": "3.0", "Last": "80"}},
        {"ticker": "CCC", "columns": {"RVOL": "bad"}}]}}}
    assert [r["symbol"] for r in parse_scan(payload)] == ["BBB", "AAA"]


def test_orb_sip_breakout_signal():
    from ctbot.strategies.orb_sip import OrbStocksInPlay

    class _Scan:
        def today(self):
            return [{"symbol": "XYZ", "rvol_ratio": 0.2, "atr14": 2.0, "last": 50}]

    t0 = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)  # 9:30 ET
    green = [Bar("XYZ", t0, 50.0, 51.0, 49.8, 50.8, 1e5), Bar("XYZ", t0 + timedelta(minutes=5), 50.8, 51.2, 50.7, 51.1, 1e5)]

    class _Bars:
        def today_bars(self, syms):
            return {"XYZ": green}

    now = t0 + timedelta(minutes=11)  # 9:41 ET
    ctx = StrategyContext(_Bars(), ("SPY",), (), now.date(), frozenset(), now)
    (sig,) = OrbStocksInPlay(scanner=_Scan()).generate(ctx)
    assert sig.evidence["trigger"] == 51.0 and sig.evidence["stop"] == 51.1 - 0.2 and sig.evidence["size_pct"] == 0.2
    red = [Bar("XYZ", t0, 50.0, 51.0, 49.0, 49.5, 1e5), green[1]]
    _Bars.today_bars = lambda self, syms: {"XYZ": red}
    assert OrbStocksInPlay(scanner=_Scan()).generate(ctx) == []


def test_vol_capped_core_holds_other_fund_and_trims_when_vol_rises():
    import tempfile
    from pathlib import Path

    from ctbot.config import Settings
    from ctbot.models import Position
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import Components, run_cycle
    from ctbot.risk.manager import AccountState, RiskManager
    from ctbot.strategies.registry import WeightedStrategy
    from ctbot.strategies.swing import TrendCore

    qqq = [400 + i for i in range(200)]
    calm = [100 * (1 + 0.004 * (-1) ** k) for k in range(25)]       # ~6% annualized vol
    wild = [100 * (1 + 0.05 * (-1) ** k) for k in range(25)]         # ~80% annualized vol
    hist = {"QQQ": qqq, "QLD": calm}

    class MD:
        def daily_bars(self, sym, n):
            return [Bar(sym, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i), c, c, c, c, 1e9)
                    for i, c in enumerate(hist[sym][-n:])]

        def quotes(self, syms):
            return {s: Quote(s, 100.0 if s == "QLD" else 650.0, 100.0 if s == "QLD" else 650.0,
                             100.0 if s == "QLD" else 650.0) for s in syms}

    md = MD()
    core = TrendCore("QQQ", 170, size_pct=0.8, hold="QLD", vol_cap=0.30)
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(md, 5000.0, Path(d) / "l.json", slippage_bps=0, clock=lambda: NOW)
        s = Settings.from_env({"ENTRY_MODE": "auto", "MAX_TRADING_CAPITAL": "5000", "MAX_CORE_PCT": "0.8"})
        c = Components(s, md, [], [WeightedStrategy(core, 1.0, standalone=True)], None, RiskManager(s.risk), broker, ["QQQ"])
        st = AccountState(5000, 5000, 5000, 5000, 0)
        run_cycle(c, st, NOW)  # calm: full 80% in QLD
        assert [(p.symbol, round(p.quantity * 100)) for p in broker.positions()] == [("QLD", 4000)]
        hist["QLD"] = wild  # volatility jumps: target = 0.8 * 0.30 / ~0.8 -> ~30% of equity
        st.cash, st.equity = broker.cash(), broker.equity()
        run_cycle(c, st, NOW + timedelta(minutes=5))
        # ~160% annualized vol -> target 0.8 * 0.30 / 1.6 = ~15% of equity (~$750); bracket kept
        assert 600 < broker.positions()[0].quantity * 100 < 900 and "QLD" in st.brackets


def test_config_can_reuse_a_model_class_under_its_own_name():
    from ctbot.strategies.registry import build_strategies

    ws = build_strategies({
        "trend_core": {"enabled": True, "params": {"size_pct": 0.4}},
        "trend_core_qld": {"enabled": True, "class": "trend_core", "params": {"hold": "QLD", "vol_cap": 0.3}},
    })
    assert [(w.strategy.name, w.strategy.hold) for w in ws] == [("trend_core", "QQQ"), ("trend_core_qld", "QLD")]
