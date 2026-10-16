"""Event-driven strategies, ensemble, and the JSON provider. Stdlib-only (no numpy)."""

from datetime import date
from pathlib import Path

from ctbot.aggregation.ensemble import EnsembleParams, combine
from ctbot.models import AltDataEvent, Direction, EventKind, Side, Signal
from ctbot.providers.json_file import JsonFileProvider
from ctbot.providers.registry import build_providers, fetch_all
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.event_cluster import CongressionalStrategy, CopyTradingStrategy
from ctbot.strategies.options_flow import OptionsFlowStrategy

TODAY = date(2026, 10, 13)


def _ev(kind, sym, side, actor, disclosed, traded=None, size=50_000, raw=None):
    return AltDataEvent(
        kind, sym, side, actor, traded or disclosed, disclosed, "test", size, raw or {}
    )


def _ctx(events, held=frozenset()):
    return StrategyContext(None, ("MSFT", "JPM"), tuple(events), TODAY, held)


def test_congressional_needs_multiple_members_and_fresh_disclosures():
    events = [
        _ev(EventKind.CONGRESS_TRADE, "MSFT", Side.BUY, "A", date(2026, 10, 1)),
        _ev(EventKind.CONGRESS_TRADE, "MSFT", Side.BUY, "B", date(2026, 10, 5)),
        _ev(EventKind.CONGRESS_TRADE, "JPM", Side.BUY, "A", date(2026, 10, 5)),
        # stale: traded 90 days before disclosure
        _ev(EventKind.CONGRESS_TRADE, "JPM", Side.BUY, "C", date(2026, 10, 5), date(2026, 7, 7)),
    ]
    sigs = CongressionalStrategy(min_actors=2).generate(_ctx(events))
    assert [s.symbol for s in sigs] == ["MSFT"]
    assert sigs[0].direction is Direction.LONG


def test_bearish_cluster_only_emits_exit_for_held():
    events = [
        _ev(EventKind.CONGRESS_TRADE, "MSFT", Side.SELL, a, date(2026, 10, 5)) for a in "ABC"
    ]
    assert CongressionalStrategy().generate(_ctx(events)) == []
    sigs = CongressionalStrategy().generate(_ctx(events, frozenset({"MSFT"})))
    assert sigs[0].direction is Direction.EXIT


def test_copy_trading_filters_to_tracked_actors():
    events = [
        _ev(EventKind.INVESTOR_TRADE, "JPM", Side.BUY, "Tracked Fund", date(2026, 10, 10)),
        _ev(EventKind.INVESTOR_TRADE, "MSFT", Side.BUY, "Random", date(2026, 10, 10)),
    ]
    sigs = CopyTradingStrategy(tracked_actors=["tracked fund"]).generate(_ctx(events))
    assert [s.symbol for s in sigs] == ["JPM"]


def test_copy_trading_with_no_tracked_actors_follows_nobody():
    events = [_ev(EventKind.INVESTOR_TRADE, "JPM", Side.BUY, "Anyone", date(2026, 10, 10))]
    assert CopyTradingStrategy().generate(_ctx(events)) == []
    assert CopyTradingStrategy(tracked_actors=[]).generate(_ctx(events)) == []


def test_copy_trading_ignores_13f_events():
    events = [
        _ev(EventKind.INSTITUTIONAL_HOLDING, "JPM", Side.BUY, "Tracked Fund", date(2026, 10, 10))
    ]
    assert CopyTradingStrategy(tracked_actors=["tracked fund"]).generate(_ctx(events)) == []


def test_options_flow_nets_premium():
    raw = {"is_sweep": True, "expiry": "2026-11-20"}
    events = [
        _ev(EventKind.OPTIONS_FLOW, "MSFT", Side.BUY, "flow", TODAY, size=400_000, raw=raw),
        _ev(EventKind.OPTIONS_FLOW, "MSFT", Side.BUY, "flow", TODAY, size=300_000, raw=raw),
        _ev(EventKind.OPTIONS_FLOW, "MSFT", Side.SELL, "flow", TODAY, size=150_000, raw=raw),
    ]
    sigs = OptionsFlowStrategy(min_net_premium_usd=500_000).generate(_ctx(events))
    assert len(sigs) == 1 and sigs[0].evidence["net_premium_usd"] == 550_000


def test_ensemble_requires_agreement_and_ranks():
    weights = {"momentum": 1.0, "congressional": 1.0, "options_flow": 1.0}
    sigs = [
        Signal("momentum", "MSFT", Direction.LONG, 0.9, 20),
        Signal("congressional", "MSFT", Direction.LONG, 0.6, 30),
        Signal("momentum", "JPM", Direction.LONG, 1.0, 20),  # single strategy only
    ]
    cands = combine(sigs, weights, EnsembleParams(min_agreeing_strategies=2))
    assert [c.symbol for c in cands] == ["MSFT"]
    assert cands[0].strategies == ("congressional", "momentum")


def test_ensemble_standalone_strategy_can_open_long_alone():
    weights = {"momentum": 1.0, "congressional": 1.0, "copy_trading": 0.5}
    params = EnsembleParams(min_long_score=0.15, min_agreeing_strategies=2)
    sigs = [
        Signal("copy_trading", "JPM", Direction.LONG, 1.0, 30),
        Signal("copy_trading", "KO", Direction.LONG, 0.1, 30),  # below min_long_score
        Signal("momentum", "MSFT", Direction.LONG, 1.0, 20),  # not standalone
    ]
    assert combine(sigs, weights, params) == []
    cands = combine(sigs, weights, params, standalone=frozenset({"copy_trading"}))
    assert [(c.symbol, c.direction) for c in cands] == [("JPM", Direction.LONG)]


def test_json_provider_reads_sample_and_registry_skips_unconfigured():
    root = Path(__file__).resolve().parents[1] / "data" / "events"
    events = JsonFileProvider(root).fetch(set(EventKind), date(2026, 1, 1))
    assert {e.kind for e in events} >= {EventKind.CONGRESS_TRADE, EventKind.OPTIONS_FLOW}

    providers = build_providers(["json_file", "quiver", "unusual_whales"], {"EVENTS_DIR": str(root)})
    assert [p.name for p in providers] == ["json_file"]  # paid providers lack keys
    assert fetch_all(providers, {EventKind.INSIDER_TRADE}, date(2026, 1, 1))


def test_decision_api_schema_meets_structured_output_rules():
    from ctbot.decision.engine import DecisionSchema, api_schema

    s = api_schema()

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object":
                assert n.get("additionalProperties") is False
            assert not {"minimum", "maximum"} & set(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(s)
    # bounds still enforced locally
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DecisionSchema.model_validate_json('{"approve": true, "confidence": 1.5, "rationale": "", "suggested_notional": 1}')
