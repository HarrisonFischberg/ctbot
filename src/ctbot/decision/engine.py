"""Claude-backed decision engine."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ctbot.decision.prompts import SYSTEM_PROMPT
from ctbot.models import Candidate, Decision

log = logging.getLogger(__name__)


class DecisionSchema(BaseModel):
    # Structured outputs require additionalProperties: false on every object.
    model_config = ConfigDict(extra="forbid")

    approve: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_notional: float = Field(ge=0.0)
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None


_UNSUPPORTED_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}


def api_schema() -> dict:
    """DecisionSchema as JSON Schema, minus keywords structured outputs rejects. The bounds are
    still enforced locally: the reply is validated against the full pydantic model."""

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYWORDS}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(DecisionSchema.model_json_schema())


_REJECT = Decision(approve=False, confidence=0.0, rationale="", suggested_notional=0.0)


def _reject(reason: str) -> Decision:
    return Decision(**{**asdict(_REJECT), "rationale": reason})


class ClaudeDecisionEngine:
    def __init__(self, model: str, effort: str = "medium", client=None) -> None:
        self.model = model
        self.effort = effort
        self._client = client  # injected in tests; created lazily otherwise

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def build_user_message(self, candidate: Candidate, account: dict) -> str:
        payload = {
            "candidate": {
                "symbol": candidate.symbol,
                "direction": candidate.direction.value,
                "ensemble_score": candidate.score,
                "signals": [
                    {
                        "strategy": s.strategy,
                        "direction": s.direction.value,
                        "strength": s.strength,
                        "horizon_days": s.horizon_days,
                        "evidence": s.evidence,
                        "ts": s.ts.isoformat(),
                    }
                    for s in candidate.signals
                ],
            },
            "account": account,
        }
        return "Review this candidate trade:\n" + json.dumps(
            payload, indent=2, sort_keys=True, default=str
        )

    def evaluate(self, candidate: Candidate, account: dict) -> Decision:
        """Any failure (API error, refusal, bad JSON) fails closed to a rejection."""
        import anthropic

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self.build_user_message(candidate, account)}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": api_schema()},
                },
            )
        except anthropic.RateLimitError as e:
            log.warning("Claude rate limited: %s", e)
            return _reject("rate_limited")
        except anthropic.APIStatusError as e:
            log.error("Claude API error %s: %s", e.status_code, e)
            return _reject(f"api_error_{e.status_code}")
        except anthropic.APIConnectionError as e:
            log.error("Claude connection error: %s", e)
            return _reject("connection_error")

        if response.stop_reason != "end_turn":
            return _reject(f"stop_reason_{response.stop_reason}")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = DecisionSchema.model_validate_json(text)
        except ValidationError as e:
            log.error("Invalid decision JSON: %s", e)
            return _reject("invalid_json")
        return Decision(**parsed.model_dump())
