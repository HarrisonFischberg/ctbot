"""Learning from each trade: track-record sizing, the pull-back after losses, the sector cap
and the trade post-mortem log."""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ctbot import risk_mode, sectors, track_record, trade_log
from ctbot.config import RiskLimits
from ctbot.models import Candidate, Decision, Direction, Position, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import run_cycle
from ctbot.risk.manager import AccountState, RiskManager

NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)  # Tue 11:00 ET
QUOTE = Quote("KO", 60.0, 60.02, 60.01)


def _fill(sym, side, qty, px, tag, days_ago=1):
    return {"symbol": sym, "side": side, "quantity": qty, "price": px, "tag": tag,
            "ts": (NOW - timedelta(days=days_ago)).isoformat()}


def _state(**kw):
    base = dict(equity=10_000, cash=10_000, peak_equity=10_000, day_start_equity=10_000, open_positions=0)
    return AccountState(**{**base, **kw})


def _cand(sym="KO", model="momentum", **evidence):
    return Candidate(sym, Direction.LONG, 0.4, (Signal(model, sym, Direction.LONG, 0.8, 20, evidence),))


def _decision(notional=10_000.0):
    return Decision(approve=True, confidence=0.8, rationale="", suggested_notional=notional)


def test_round_trips_credit_every_model_in_the_buy_tag():
    fills = [_fill("AMD", "buy", 2, 100, "options_flow,momentum"), _fill("AMD", "sell", 2, 94, "stop_loss"),
             _fill("MU", "buy", 1, 100, "options_flow"), _fill("MU", "sell", 1, 103, "trailing_stop")]
    rets = track_record.returns_by_model(fills)
    assert [round(r, 2) for r in rets["options_flow"]] == [-0.06, 0.03]
    assert [round(r, 2) for r in rets["momentum"]] == [-0.06]


def test_few_trades_barely_move_the_size():
    # one -6% trade, pulled toward zero by 10 flat ones: -0.55% avg -> 0.86x, not 0.25x
    assert track_record.multiplier([-0.06], 10, 0.25, 1.25) == 0.864
    assert track_record.multiplier([-0.05] * 30, 10, 0.25, 1.25) == 0.25   # a long losing record bottoms out
    assert track_record.multiplier([0.02] * 30, 10, 0.25, 1.25) == 1.25    # and a winning one is capped
    assert track_record.multiplier([], 10, 0.25, 1.25) == 1.0


def test_old_trades_age_out_of_the_window():
    fills = [_fill("AMD", "buy", 1, 100, "orb", 40), _fill("AMD", "sell", 1, 90, "stop_loss", 39)]
    assert track_record.size_multipliers(fills, NOW) == {}


def test_size_multiplier_shrinks_a_reviewed_entry_but_not_a_systematic_one():
    rm = RiskManager(RiskLimits(max_trading_capital=1e9))
    rm.size_mult = {"momentum": 0.5, "trend_core": 0.5}
    v = rm.check_entry(_cand(), _decision(), _state(), QUOTE, 1e9)
    assert v.orders[0].notional == 1000.0  # 20% of 10k, halved
    v = rm.check_entry(_cand(model="trend_core", size_pct=0.2), _decision(), _state(), QUOTE, 1e9)
    assert v.orders[0].notional == 2000.0  # systematic size is fixed


def test_pull_back_triggers():
    L = RiskLimits()
    assert risk_mode.reasons(L, 9_650, 10_000, 10_000, 700, 720) == [
        "down 3.5% today", "QQQ under its 50-day average"]
    assert risk_mode.reasons(L, 9_400, 9_400, 10_000, 730, 720) == ["6.0% below its best"]
    assert risk_mode.reasons(L, 10_000, 10_000, 10_000, 730, 720) == []
    assert risk_mode.reasons(L, 10_000, 10_000, 10_000, None, 720) == []  # no price: no trend trigger


def test_pulled_back_buys_cannot_borrow():
    rm = RiskManager(RiskLimits(max_trading_capital=1e9))
    borrowing = dict(cash=-2_000, buying_power=8_000)
    assert rm.check_entry(_cand(), _decision(), _state(**borrowing), QUOTE, 1e9).allowed
    v = rm.check_entry(_cand(), _decision(), _state(**borrowing, risk_off=["down 3.5% today"]), QUOTE, 1e9)
    assert v.reasons == ("pulled_back_no_borrowing",)
    v = rm.check_entry(_cand(), _decision(), _state(cash=500, buying_power=8_000, risk_off=["x"]), QUOTE, 1e9)
    assert v.orders[0].notional == 500.0  # cash on hand only


def test_sector_cap_counts_index_funds_and_leverage():
    exp = sectors.exposure({"QQQ": 1_000, "QLD": 1_000, "AMD": 1_000, "KO": 500})
    assert exp == {"Technology": 4_000, "Staples": 500}
    rm = RiskManager(RiskLimits(max_trading_capital=1e9))
    full = _state(sector_exposure={"Technology": 4_000})
    v = rm.check_entry(_cand("AMD"), _decision(), full, Quote("AMD", 500, 500.1, 500), 1e9)
    assert v.reasons == ("sector_cap_technology",)
    some_room = _state(sector_exposure={"Technology": 3_500})
    v = rm.check_entry(_cand("AMD"), _decision(), some_room, Quote("AMD", 500, 500.1, 500), 1e9)
    assert v.orders[0].notional == 500.0  # tops the sector up to 40%, no further
    assert rm.check_entry(_cand("KO"), _decision(), full, QUOTE, 1e9).allowed  # other sectors unaffected


class _MD:
    def __init__(self, px):
        self.px = px

    def daily_bars(self, symbol, lookback_days):
        from ctbot.models import Bar
        return [Bar(symbol, NOW - timedelta(days=1), 50, 50, 50, 50, 1e7)] * min(lookback_days, 60)

    def quotes(self, symbols):
        return {s: Quote(s, self.px, self.px, self.px) for s in symbols}


def test_stop_exit_writes_a_post_mortem_flagging_the_gap():
    from tests.test_pipeline import _components

    with tempfile.TemporaryDirectory() as d:
        md = _MD(46.0)
        broker = PaperBroker(md, 200.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        broker._positions["KO"] = Position("KO", 1.0, 50.0, NOW - timedelta(days=1))
        c = _components(broker, [])
        c.market_data = md
        state = AccountState(equity=200, cash=broker.cash(), peak_equity=200, day_start_equity=200,
                             open_positions=1)
        state.brackets["KO"] = {"stop": 48.0, "target": None, "model": "orb", "hi": 51.0, "lo": 49.0,
                                "ctx": {"models": ["orb"], "confidence": 0.7}}
        run_cycle(c, state, NOW)
        [rec] = trade_log.read(c.settings.trade_log_path)
    assert rec["symbol"] == "KO" and rec["exit_reason"] == "stop_loss" and rec["pnl"] == -4.0
    assert rec["models"] == ["orb"] and rec["context"]["confidence"] == 0.7
    assert rec["max_up"] == 0.02 and rec["max_down"] == -0.08  # the 46 bid seen this cycle
    assert rec["below_stop"] == -0.0417 and "below its stop" in rec["note"]


def test_buy_records_entry_context():
    from ctbot.strategies.registry import WeightedStrategy
    from tests.test_pipeline import _components, _Strategy

    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)
    with tempfile.TemporaryDirectory() as d:
        md = _MD(50.0)
        broker = PaperBroker(md, 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)])
        c.market_data = md
        state = AccountState(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
        run_cycle(c, state, NOW)
        saved = json.dumps(state.brackets)  # persisted with the risk state
    ctx = json.loads(saved)["KO"]["ctx"]
    assert ctx["models"] == ["copy_trading"] and ctx["sector"] == "Staples"
    assert ctx["size_mult"] == 1.0 and ctx["pulled_back"] == []
    assert ctx["market_vs_trend"] == 0.0 and ctx["stock_day_move"] == 0.0
