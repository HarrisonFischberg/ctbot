"""Bub's thoughts: deduped, rate-limited notes, written before a buy, never changing a trade."""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ctbot import thoughts
from ctbot.config import Settings
from ctbot.models import Bar, Direction, Quote, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import run_cycle
from ctbot.risk.manager import AccountState
from ctbot.strategies.registry import WeightedStrategy
from tests.test_pipeline import _components, _Strategy

NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)  # Tue 11:00 ET


def test_same_thought_is_said_once_a_day():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        assert thoughts.note(p, NOW, "market", "k", "hello")
        assert thoughts.note(p, NOW + timedelta(hours=2), "market", "k", "hello") is None
        assert thoughts.note(p, NOW + timedelta(days=1), "market", "k", "hello")  # a new trading day
        assert len(thoughts.read(p)) == 2


def test_family_note_repeats_only_when_the_state_changes():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        assert thoughts.note(p, NOW, "caution", "below", "under", family="trend")
        assert thoughts.note(p, NOW + timedelta(days=3), "caution", "below", "under", family="trend") is None
        assert thoughts.note(p, NOW + timedelta(days=3), "market", "above", "over", family="trend")
        assert thoughts.note(p, NOW + timedelta(days=4), "caution", "below", "under", family="trend")


def test_market_notes_are_capped_per_hour_but_buy_notes_are_not():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        said = [thoughts.note(p, NOW + timedelta(minutes=i), "market", f"k{i}", "x") for i in range(5)]
        assert sum(1 for s in said if s) == thoughts.MAX_PER_HOUR
        assert thoughts.note(p, NOW + timedelta(minutes=6), "trade", "buy_KO", "Buying KO", capped=False)
        assert thoughts.note(p, NOW + timedelta(minutes=70), "market", "k9", "x")  # a fresh hour


class _MD:
    """QQQ at 95 against a 100 trend, 2% under yesterday's close."""

    def daily_bars(self, symbol, lookback_days):
        close = 96.94 if symbol == "QQQ" else 50.0
        return [Bar(symbol, NOW - timedelta(days=1), close, close, close, close, 1e7)] * min(lookback_days, 60)

    def quotes(self, symbols):
        return {s: Quote(s, 95.0, 95.0, 95.0) if s == "QQQ" else Quote(s, 51.0, 51.0, 51.0) for s in symbols}


def _ideas(path, reason, n):
    with path.open("w") as fh:
        for _ in range(n):
            fh.write(json.dumps({"ts": NOW.isoformat(), "status": "rejected",
                                 "risk": {"allowed": False, "reasons": [reason]}}) + "\n")


def test_observe_says_what_it_sees_once():
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [])
        c.market_data = _MD()
        c.trend.day, c.trend.avg = NOW.date(), 100.0
        c.risk.size_mult = {"orb": 0.4}
        _ideas(c.settings.ideas_path, "spread_too_wide", 6)
        state = AccountState(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
        thoughts.observe(c, state, NOW)
        first = thoughts.read(c.settings.thoughts_path)
        thoughts.observe(c, state, NOW + timedelta(minutes=5))
        again = thoughts.read(c.settings.thoughts_path)
    keys = {t["key"] for t in first}
    assert "trend_below" in keys and "QQQ_down_big" in keys
    assert len(first) == thoughts.MAX_PER_HOUR  # four things to say, three fit this hour
    trend = next(t for t in first if t["key"] == "trend_below")
    assert "5.0% under its 50-day average" in trend["text"]
    assert len(again) == len(first)  # nothing repeated, and the cap still holds


def test_rejection_pattern_names_the_reason():
    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [])
        _ideas(c.settings.ideas_path, "spread_too_wide", 6)
        state = AccountState(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
        thoughts.observe(c, state, NOW)  # the plain test market: flat, no trend set
        [t] = thoughts.read(c.settings.thoughts_path)
    assert t["key"] == "rejects_spread_too_wide" and t["text"].startswith("6 of today's 6 ideas")


def test_buy_is_announced_before_the_fill_and_trades_the_same():
    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 30)

    def run(think):
        with tempfile.TemporaryDirectory() as d:
            broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
            c = _components(broker, [WeightedStrategy(_Strategy("copy_trading", [sig]), 0.5, standalone=True)])
            c.market_data, c.think = _MD(), think
            state = AccountState(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
            run_cycle(c, state, NOW)
            said = thoughts.read(c.settings.thoughts_path)
            fills = json.loads(broker.ledger_path.read_text())["fills"]
        return said, [(f["symbol"], f["side"], f["quantity"]) for f in fills]

    said, fills = run(True)
    quiet, quiet_fills = run(False)
    assert fills == quiet_fills and fills  # the note changes nothing about the trade
    assert quiet == []
    buy = next(t for t in said if t["kind"] == "trade")
    assert buy["symbol"] == "KO" and buy["models"] == ["copy_trading"]
    assert buy["text"].startswith("Buying KO") and "{models} picked it" in buy["text"]
    assert datetime.fromisoformat(buy["ts"]) <= NOW


def test_a_broken_thought_never_blocks_trading():
    class Boom:
        def quotes(self, symbols):
            raise RuntimeError("no prices")

    with tempfile.TemporaryDirectory() as d:
        broker = PaperBroker(_MD(), 250.0, Path(d) / "ledger.json", slippage_bps=0, clock=lambda: NOW)
        c = _components(broker, [])
        c.market_data = Boom()
        state = AccountState(equity=250, cash=250, peak_equity=250, day_start_equity=250, open_positions=0)
        thoughts.observe(c, state, NOW)  # logs and returns
        assert thoughts.read(c.settings.thoughts_path) == []


def test_dashboard_shows_the_latest_thoughts():
    from ctbot.dashboard.state import build_state

    with tempfile.TemporaryDirectory() as d:
        s = Settings.from_env({"PAPER_LEDGER_PATH": str(Path(d) / "ledger.json")})
        thoughts.note(s.thoughts_path, NOW, "caution", "k", "QQQ under its trend")
        st = build_state(s, now=NOW)
    assert st["thoughts"][0]["text"] == "QQQ under its trend" and st["thoughts"][0]["kind"] == "caution"
