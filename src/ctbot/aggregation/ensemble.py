"""Weighted signal ensemble.

score(symbol) = sum(weight_s * strength_s * sign_s) / sum(weight of all enabled strategies)
with sign +1 for LONG and -1 for EXIT/SHORT. Score is in [-1, 1]. Requiring agreement
from several independent strategies is the main defense against any one noisy source.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from ctbot.models import Candidate, Direction, Signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnsembleParams:
    min_long_score: float = 0.15
    min_exit_score: float = 0.10
    min_agreeing_strategies: int = 2
    max_candidates: int = 5  # caps Claude calls per cycle


def combine(
    signals: list[Signal],
    weights: dict[str, float],
    params: EnsembleParams,
    standalone: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """`standalone` strategies (e.g. copy_trading) can open a LONG alone: their own signal
    strength is checked against min_long_score, skipping the agreement and weighting rules.
    Claude review and risk checks still apply downstream.
    """
    total_weight = sum(weights.values()) or 1.0
    by_symbol: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        if s.strategy in weights:
            by_symbol[s.symbol].append(s)

    candidates: list[Candidate] = []
    for sym, sigs in by_symbol.items():
        raw = sum(
            weights[s.strategy] * s.strength * (1 if s.direction is Direction.LONG else -1)
            for s in sigs
        )
        score = raw / total_weight
        if score > 0:
            agreeing = {s.strategy for s in sigs if s.direction is Direction.LONG}
            solo = any(
                s.strategy in standalone
                and s.direction is Direction.LONG
                and s.strength >= params.min_long_score
                for s in sigs
            )
            ensemble_ok = (
                score >= params.min_long_score
                and len(agreeing) >= params.min_agreeing_strategies
            )
            if not (ensemble_ok or solo):
                continue
            direction = Direction.LONG
        else:
            # Exits need less agreement: reducing risk should be easy.
            if -score < params.min_exit_score:
                continue
            direction = Direction.EXIT
        candidates.append(Candidate(sym, direction, round(score, 4), tuple(sigs)))

    exits = [c for c in candidates if c.direction is Direction.EXIT]
    longs = sorted(
        (c for c in candidates if c.direction is Direction.LONG), key=lambda c: c.score, reverse=True
    )
    # The cap limits paid Claude reviews; rule-based (systematic) entries cost nothing and always pass.
    rule = [c for c in longs if all(s.evidence.get("systematic") for s in c.signals if s.direction is Direction.LONG)]
    reviewed = [c for c in longs if c not in rule]
    if len(reviewed) > params.max_candidates:
        log.info("over %d ideas this cycle; not reviewing %s", params.max_candidates,
                 ", ".join(c.symbol for c in reviewed[params.max_candidates:]))
    return exits + rule + reviewed[: params.max_candidates]
