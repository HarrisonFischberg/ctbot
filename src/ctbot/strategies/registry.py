"""Build enabled strategies and their ensemble weights from config/strategies.toml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
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
from ctbot.strategies.intraday import IntradayMomentum, OpeningRangeBreakout, OvernightHold, VwapReversion
from ctbot.strategies.mean_reversion import MeanReversionStrategy
from ctbot.strategies.orb_sip import OrbStocksInPlay
from ctbot.strategies.rule import RuleStrategy
from ctbot.strategies.swing import Rsi2Etf, TrendCore, TurnOfMonth
from ctbot.strategies.trend_template import TrendTemplate
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
    "intraday_momentum": IntradayMomentum,
    "rsi2_etf": Rsi2Etf,
    "turn_of_month": TurnOfMonth,
    "trend_core": TrendCore,
    "orb_sip": OrbStocksInPlay,
    "trend_template": TrendTemplate,
    "rule": RuleStrategy,
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
    """strategies.toml plus the strategy library (ctbot.library): one watch-only entry per family
    and universe, unless the file has `[library] enabled = false` or its own table of that name."""
    import tomllib  # Python 3.11+

    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    return with_library(cfg)


def with_library(cfg: dict[str, Any]) -> dict[str, Any]:
    from ctbot.library import signals as sg

    cfg = dict(cfg)
    lib = cfg.pop("library", {})
    if not lib.get("enabled", True):
        return cfg
    for fam in sg.FAMILIES:
        for uni in sg.UNIVERSES:
            cfg.setdefault(sg.entry_name(fam, uni), {"enabled": True, "mode": "shadow", "standalone": True,
                                                     "class": "rule", "params": {"family": fam, "universe": uni}})
    return cfg


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


def with_modes(strategies: list[WeightedStrategy], config: dict[str, dict[str, Any]]) -> list[WeightedStrategy]:
    """`strategies` with each one's shadow/trade mode re-read from `config` (nothing else changes,
    so a running paper bot can pick up a mode switch without rebuilding its models)."""
    out = []
    for ws in strategies:
        mode = str((config.get(ws.strategy.name) or {}).get("mode", "trade")).lower()
        out.append(replace(ws, mode=mode) if mode in ("trade", "shadow") and mode != ws.mode else ws)
    return out


def set_mode(path: Path, name: str, mode: str) -> None:
    """Rewrite one table's `mode = "..."` line in strategies.toml (or add one under its header),
    keeping every comment and the rest of the file as it is. Atomic: the bot never reads half."""
    import os
    import re
    import tomllib

    if mode not in ("trade", "shadow"):
        raise ValueError("mode must be trade or shadow")
    lines = path.read_text().splitlines(keepends=True)
    head = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{name}]"), None)
    if head is None:
        raise ValueError(f"no [{name}] table in {path}")
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(r'^(\s*mode\s*=\s*)"[^"]*"(.*)$', re.S)
    for i in range(head + 1, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f'{m.group(1)}"{mode}"{m.group(2)}'
            break
    else:
        lines.insert(head + 1, f'mode = "{mode}"\n')
    text = "".join(lines)
    try:  # never leave a broken file: check the result before it replaces the old one
        ok = tomllib.loads(text)[name].get("mode") == mode
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise ValueError(f"could not set [{name}] mode in {path}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def required_event_kinds(strategies: list[WeightedStrategy]) -> set[EventKind]:
    kinds: set[EventKind] = set()
    for ws in strategies:
        kinds |= set(ws.strategy.event_kinds)
    return kinds
