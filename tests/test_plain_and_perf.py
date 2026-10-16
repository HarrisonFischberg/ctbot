"""Plain-English strategy cards and the 3/6/12-month performance windows."""

from datetime import date, timedelta
from pathlib import Path

from ctbot import allocations
from ctbot.dashboard.plain import describe
from ctbot.research.perf import _window_stats
from ctbot.strategies.registry import load_strategy_config

ROOT = Path(__file__).resolve().parents[1]


def test_every_configured_strategy_reads_in_plain_words():
    cfg = load_strategy_config(ROOT / "config" / "strategies.toml")
    for name, t in cfg.items():
        d = describe(name, t.get("class"), t.get("params", {}))
        assert d["title"] and d["tagline"] and d["risk"] in ("Lower", "Medium", "High")
        labels = " ".join(s["label"] for s in d["settings"])
        assert "_" not in labels, (name, labels)  # no raw parameter names like sma_days
    qld = describe("trend_core_qld", "trend_core", cfg["trend_core_qld"]["params"])
    assert qld["title"] == "Nasdaq-100 Trend, 2× leveraged" and qld["risk"] == "High"
    assert {"label": "Fund it buys", "value": "2× Nasdaq-100 (QLD)"} in qld["settings"]


def test_window_stats_returns_and_worst_drop():
    end = date(2026, 10, 13)
    curve = [(end - timedelta(days=400 - i), 1000.0 + i) for i in range(401)]
    curve[-50] = (curve[-50][0], 900.0)  # a dip inside the last 3 months
    w = _window_stats(curve)
    assert round(w["1Y"]["return"], 4) == round(1400 / 1035 - 1, 4)
    assert w["3M"]["worst_drop"] < -0.3
    short = [(end - timedelta(days=100 - i), 1000.0) for i in range(101)]
    assert _window_stats(short)["1Y"] is None and _window_stats(short)["3M"] is not None


def test_run_limits_are_validated(tmp_path):
    f = tmp_path / "a.json"
    run = allocations.start(f, "trend_core", 1000, 5000, max_loss_pct=0.15, take_profit_pct=0.3)
    assert run["max_loss_pct"] == 0.15 and run["realized"] == 0.0
    allocations.add_realized(f, "trend_core", -20.5)
    assert allocations.load(f)["trend_core"]["realized"] == -20.5
    import pytest

    with pytest.raises(ValueError):
        allocations.start(f, "orb", 100, 5000, max_loss_pct=2.0)
