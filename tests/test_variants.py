"""Side-by-side paper books: config validation, shared data and reviews, independent ledgers."""

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
