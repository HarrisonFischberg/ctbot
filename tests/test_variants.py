"""Side-by-side paper books: config validation, shared data and reviews, independent ledgers."""

import json
from pathlib import Path

import pytest

from ctbot import variants
from ctbot.config import Settings
from ctbot.models import Decision, Direction, Signal
from ctbot.paper.simulator import PaperBroker
from ctbot.pipeline import Components, run_cycle
from ctbot.risk.manager import AccountState, RiskManager
from ctbot.strategies.registry import WeightedStrategy

from test_pipeline import NOW, _MD, _Strategy


def _write(tmp_path, text):
    p = tmp_path / "variants.toml"
    p.write_text(text)
    return p


def test_load_validates_names_settings_and_ranges(tmp_path):
    ok = variants.load(_write(tmp_path, '[a_1]\ntitle = "A"\nautopilot = { giveback_pct = 0.04 }\n'
                                        'env = { MAX_OPEN_POSITIONS = 4 }\n'))
    assert ok[0].name == "a_1" and ok[0].autopilot.giveback_pct == 0.04 and ok[0].autopilot.enabled
    assert ok[0].env == {"MAX_OPEN_POSITIONS": "4"}
    assert variants.load(_write(tmp_path, "enabled = false\n[a]\n")) == []
    assert variants.load(tmp_path / "missing.toml") == []
    for bad in ('[a]\nenv = { LIVE_TRADING_ENABLED = "true" }\n', '[a]\nenv = { PAPER_LEDGER_PATH = "x" }\n',
                '[a]\nenv = { ANTHROPIC_API_KEY = "x" }\n', '["A b"]\n', '[a]\nautopilot = { giveback_pct = 0.9 }\n'):
        with pytest.raises(ValueError):
            variants.load(_write(tmp_path, bad))


def test_factory_book_loads_and_reloads_its_models(tmp_path, monkeypatch):
    from ctbot.library import factory

    (v,) = variants.load(_write(tmp_path, '[f]\nstrategies = "factory"\n'))
    assert v.strategies == "factory"
    with pytest.raises(ValueError):
        variants.load(_write(tmp_path, '[f]\nstrategies = "everything"\n'))
    report = tmp_path / "factory_report.json"
    monkeypatch.setattr(factory, "REPORT", report)
    c = Components(settings=None, market_data=None, providers=[], strategies=[], engine=None,
                   risk=None, executor=None, universe=[])
    c.configured, c.launched = [], ("autopilot", frozenset())
    b = variants.Book(v, c, None, None, None)
    variants._reload_factory(b)
    assert c.strategies == [] and c.launched is not None  # no report yet: nothing changes
    cand = {"name": "fx_rsi2_any_g5_h5_etfs", "family": "rsi2", "filter": "any", "giveback": 0.05,
            "max_hold": 5, "universe": "etfs"}
    report.write_text(json.dumps({"live": [cand["name"]], "candidates": {cand["name"]: cand}}))
    variants._reload_factory(b)
    (ws,) = c.strategies
    assert ws.strategy.name == cand["name"] and ws.standalone and ws.mode == "trade"
    assert ws.strategy.family.max_hold == 5 and ws.strategy.evidence == {"systematic": True, "giveback": 0.05}
    assert c.configured is None and c.launched is None  # the autopilot re-launches from the new list


def test_book_env_is_always_paper_in_its_own_folder():
    v = variants.Variant("x", "X", variants.autopilot.AutopilotConfig(enabled=True))
    env = variants.book_env({"LIVE_TRADING_ENABLED": "true", "CTBOT_MODE": "live"}, Path("data/paper_ledger.json"), v)
    s = Settings.from_env(env)
    assert not s.live_trading_enabled and s.mode.value == "paper" and s.entry_mode == "auto"
    assert s.paper_ledger_path == Path("data/variants/x/paper_ledger.json")
    assert s.autopilot_path == Path("data/variants/x/autopilot.json")


def test_provider_fetched_once_per_cycle():
    class P:
        name, supports, calls = "p", frozenset({"x"}), 0

        def fetch(self, kinds, since):
            P.calls += 1
            return ["e"]

    (p,) = variants.cached([P()])
    assert p.fetch({"x"}, None) == p.fetch({"x"}, None) == ["e"] and P.calls == 1
    assert variants.cached([p])[0] is p


class _Engine:
    calls = 0

    def __init__(self, model="m", confidence=0.42):
        self.model, self.effort, self.confidence = model, "medium", confidence

    def evaluate(self, candidate, account):
        _Engine.calls += 1
        return Decision(True, self.confidence, "", 500.0)


def test_books_share_reviews_and_data_but_keep_their_own_settings_and_ledgers(tmp_path):
    """Claude says yes at 42%: the main book and a 30%-floor book buy, a 55%-floor book doesn't,
    and the three Claude reviews collapse into one."""
    (tmp_path / "strategies.toml").write_text("[library]\nenabled = false\n")
    (tmp_path / "variants.toml").write_text(
        'enabled = true\n'
        '[floor_30]\nautopilot = { min_confidence = 0.30 }\n'
        'env = { STARTING_CAPITAL = "10000", MAX_TRADING_CAPITAL = "10000" }\n'
        '[floor_55]\nautopilot = { min_confidence = 0.55 }\n'
        'env = { STARTING_CAPITAL = "10000", MAX_TRADING_CAPITAL = "10000" }\n')
    main_ledger = tmp_path / "main" / "paper_ledger.json"
    s = Settings.from_env({"ENTRY_MODE": "auto", "PAPER_LEDGER_PATH": str(main_ledger),
                           "STRATEGIES_FILE": str(tmp_path / "strategies.toml"),
                           "STARTING_CAPITAL": "10000", "MAX_TRADING_CAPITAL": "10000"})
    variants.autopilot.save(s.autopilot_path, variants.autopilot.AutopilotConfig(enabled=True))
    md = _MD()
    broker = PaperBroker(md, 10_000.0, main_ledger, slippage_bps=0, clock=lambda: NOW)
    reviews: dict = {}
    sig = Signal("copy_trading", "KO", Direction.LONG, 1.0, 1)
    main = Components(settings=s, market_data=md, providers=variants.cached([]),
                      strategies=[WeightedStrategy(_Strategy("copy_trading", [sig]), 1.0, standalone=True)],
                      engine=variants.SharedReviews(_Engine(), reviews), risk=RiskManager(s.risk),
                      executor=broker, universe=[])
    books = variants.build(main, reviews)
    assert [b.variant.name for b in books] == ["floor_30", "floor_55"]
    for b in books:
        b.components.strategies = [WeightedStrategy(_Strategy("copy_trading", [sig]), 1.0, standalone=True)]
        b.components.engine = variants.SharedReviews(_Engine(), reviews)
        b.broker.clock = lambda: NOW
        b.broker.slippage_bps = 0
        assert b.settings.autopilot_path.exists()
    state = AccountState(10_000, 10_000, 10_000, 10_000, 0)
    _Engine.calls = 0
    run_cycle(main, state, NOW)
    variants.run(books, main, NOW, lambda settings, br: None)

    assert {p.symbol for p in broker.positions()} == {"KO"}
    held = {b.variant.name: {p.symbol for p in b.broker.positions()} for b in books}
    assert held == {"floor_30": {"KO"}, "floor_55": set()}
    assert _Engine.calls == 1
    assert books[0].settings.paper_ledger_path.parent == tmp_path / "main" / "variants" / "floor_30"
    assert books[0].settings.ideas_path.exists() and books[1].settings.ideas_path.exists()

    rows = variants.summary(s, tmp_path / "variants.toml")
    assert {r["name"] for r in rows} == {"main", "floor_30", "floor_55"}


def test_data_halt_stops_entries_in_every_book(tmp_path):
    main_halt, book_halt = tmp_path / "HALT", tmp_path / "b" / "HALT"
    book_halt.parent.mkdir()
    main_halt.write_text("")
    variants._sync_halt(main_halt, book_halt)
    assert book_halt.exists()
    main_halt.unlink()
    variants._sync_halt(main_halt, book_halt)
    assert not book_halt.exists()
    book_halt.write_text("my own")  # a book's own HALT stays
    variants._sync_halt(main_halt, book_halt)
    assert book_halt.exists()


def test_books_share_one_price_fetch_and_keep_missing_methods_missing():
    from datetime import datetime, timezone

    from ctbot.models import Quote

    class Client:
        calls = 0

        def quotes(self, symbols):
            Client.calls += 1
            return {s: Quote(s, 10, 10, 10, datetime.now(timezone.utc)) for s in symbols if s != "NOPE"}

        def today_bars(self, symbols, ttl_s=60.0):
            Client.calls += 1
            return {"KO": []}

    md = variants.BookMarketData(Client())
    assert set(md.quotes(["KO", "pep"])) == {"KO", "PEP"}
    md.quotes(["KO"]), md.quotes(["PEP", "KO", "NOPE"])
    assert Client.calls == 2  # NOPE was new; KO and PEP came from the snapshot
    md.today_bars(["KO"]), md.today_bars(["KO"])
    assert Client.calls == 3
    assert not hasattr(variants.BookMarketData(object()), "today_bars")  # orb_sip checks hasattr


def test_review_cap_goes_to_ideas_that_can_still_trade():
    from ctbot.aggregation.ensemble import EnsembleParams, combine

    sigs = [Signal("copy_trading", s, Direction.LONG, st, 1) for s, st in (("A", 0.9), ("B", 0.8), ("C", 0.7))]
    params = EnsembleParams(max_candidates=2)
    cands = combine(sigs, {"copy_trading": 1.0}, params, frozenset({"copy_trading"}))
    assert [c.symbol for c in cands] == ["A", "B"]
    cands = combine(sigs, {"copy_trading": 1.0}, params, frozenset({"copy_trading"}), lambda c: c.symbol == "A")
    assert [c.symbol for c in cands] == ["B", "C"]


def test_book_is_only_called_better_once_the_edge_is_steady(tmp_path):
    import json
    from datetime import date, timedelta

    def history(path, daily):
        path.parent.mkdir(parents=True, exist_ok=True)
        eq, lines = 10_000.0, []
        for i, r in enumerate(daily):
            eq *= 1 + r
            lines.append(json.dumps({"ts": f"{date(2026, 10, 1) + timedelta(days=i)}T16:00:00-04:00", "equity": eq}))
        path.write_text("\n".join(lines))

    noise = [0.01, -0.01, 0.004, -0.006, 0.008, -0.002, 0.003, -0.009, 0.005, -0.001] * 3
    history(tmp_path / "equity_history.jsonl", noise)
    history(tmp_path / "variants" / "steady" / "equity_history.jsonl", [r + 0.002 for r in noise])
    history(tmp_path / "variants" / "lucky" / "equity_history.jsonl", [r + (0.01 if i % 2 else -0.009)
                                                                      for i, r in enumerate(noise)])
    history(tmp_path / "variants" / "new" / "equity_history.jsonl", [r + 0.002 for r in noise[:5]])
    rows = [{"name": "main"}, {"name": "steady"}, {"name": "lucky"}, {"name": "new"}]
    variants._vs_main(rows, tmp_path)
    v = {r["name"]: r["vs_main"] for r in rows[1:]}
    assert v["steady"]["verdict"] == "better" and v["steady"]["days"] == 29
    assert v["lucky"]["verdict"] == "too early"
    assert v["new"] == {"days": 4, "edge": v["new"]["edge"], "verdict": "too early"}
