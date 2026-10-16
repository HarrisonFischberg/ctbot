"""Build enabled strategies and their ensemble weights from config/strategies.toml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctbot.models import EventKind
from ctbot.risk.manager import LIQUIDITY_OVERRIDE_KEYS
from ctbot.strategies.base import Strategy
from ctbot.strategies.event_cluster import (
    CongressionalStrategy,
    CopyTradingStrategy,
    InstitutionalStrategy,
)
from ctbot.strategies.intraday import OpeningRangeBreakout, OvernightHold, VwapReversion
from ctbot.strategies.mean_reversion import MeanReversionStrategy
from ctbot.strategies.orb_sip import OrbStocksInPlay
from ctbot.strategies.swing import Rsi2Etf, TrendCore, TurnOfMonth
from ctbot.strategies.momentum import MomentumStrategy
from ctbot.strategies.options_flow import OptionsFlowStrategy

log = logging.getLogger(__name__)

STRATEGY_CLASSES: dict[str, type] = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "congressional": CongressionalStrategy,
    "institutional": InstitutionalStrategy,
    "copy_trading": CopyTradingStrategy,
    "options_flow": OptionsFlowStrategy,
    "orb": OpeningRangeBreakout,
    "vwap_revert": VwapReversion,
    "overnight": OvernightHold,
    "rsi2_etf": Rsi2Etf,
    "turn_of_month": TurnOfMonth,
    "trend_core": TrendCore,
    "orb_sip": OrbStocksInPlay,
}


@dataclass(frozen=True)
class WeightedStrategy:
    strategy: Strategy
    weight: float
    standalone: bool = False  # may open a LONG without other strategies agreeing
    # Relaxed liquidity limits for this strategy's entries; keys in LIQUIDITY_OVERRIDE_KEYS.
    risk_overrides: dict[str, float] = field(default_factory=dict)
    # trade: entries are executed | shadow: reviewed, risk-checked and scored, never executed
    mode: str = "trade"


def _risk_overrides(name: str, table: dict[str, Any]) -> dict[str, float]:
    bad = set(table) - LIQUIDITY_OVERRIDE_KEYS
    if bad:
        allowed = sorted(LIQUIDITY_OVERRIDE_KEYS)
        raise ValueError(f"[{name}.risk] may only set {allowed}: {sorted(bad)}")
    out = {k: float(v) for k, v in table.items()}
    if any(v < 0 for v in out.values()):
        raise ValueError(f"[{name}.risk] values must be >= 0")
    return out


def strategy_risk_overrides(strategies: list[WeightedStrategy]) -> dict[str, dict[str, float]]:
    return {ws.strategy.name: ws.risk_overrides for ws in strategies if ws.risk_overrides}


def load_strategy_config(path: Path) -> dict[str, dict[str, Any]]:
    import tomllib  # Python 3.11+

    with path.open("rb") as fh:
        return tomllib.load(fh)


def build_strategies(config: dict[str, dict[str, Any]]) -> list[WeightedStrategy]:
    out: list[WeightedStrategy] = []
    for name, table in config.items():
        table = dict(table)
        enabled = table.pop("enabled", False)
        weight = float(table.pop("weight", 1.0))
        standalone = bool(table.pop("standalone", False))
        risk = _risk_overrides(name, table.pop("risk", {}))
        mode = str(table.pop("mode", "trade")).lower()
        if mode not in ("trade", "shadow"):
            raise ValueError(f"[{name}] mode must be 'trade' or 'shadow', not {mode!r}")
        if not enabled:
            continue
        # `class = "trend_core"` lets a second table reuse a model under its own name
        cls = STRATEGY_CLASSES.get(str(table.pop("class", name)))
        if cls is None:
            log.warning("unknown strategy %r; skipping", name)
            continue
        params = dict(table.get("params", {}))
        if "c2:watchlist" in params.get("tracked_actors", []):
            from ctbot.research.c2_rank import load_watchlist_ids

            rest = [a for a in params["tracked_actors"] if a != "c2:watchlist"]
            params["tracked_actors"] = rest + [f"c2:{sid}" for sid in load_watchlist_ids()]
        inst = cls(**params)
        if inst.name != name:
            inst.name = name  # fills, brackets and exits are owned per configured name
        out.append(WeightedStrategy(inst, weight, standalone, risk, mode))
    return out


def required_event_kinds(strategies: list[WeightedStrategy]) -> set[EventKind]:
    kinds: set[EventKind] = set()
    for ws in strategies:
        kinds |= set(ws.strategy.event_kinds)
    return kinds
