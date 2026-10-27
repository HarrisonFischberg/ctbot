"""Claude-backed decision engine."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ctbot.decision.prompts import DEBATE_PROMPTS, SYSTEM_PROMPT
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
    def __init__(self, model: str, effort: str = "medium", client=None, usage_path=None) -> None:
        self.model = model
        self.effort = effort
        self._client = client  # injected in tests; created lazily otherwise
        self.usage_path = usage_path  # data/llm_usage.jsonl: measured spend per call

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _log_usage(self, response, purpose: str) -> None:
        if self.usage_path is None or getattr(response, "usage", None) is None:
            return
        from ctbot.llm_costs import record

        try:
            record(self.usage_path, self.model, response.usage, purpose)
        except (OSError, TypeError, ValueError):
            log.exception("could not log Claude usage")

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

    def debate(self, message: str) -> dict[str, str] | None:
        """Two short advocacy calls (for / against) at low effort, after TradingAgents' bull/bear
        researchers, bounded to one round. Any failure returns None: the review goes ahead without it."""
        out = {}
        for side, prompt in DEBATE_PROMPTS.items():
            try:
                r = self.client.messages.create(
                    model=self.model, max_tokens=2000, system=prompt,
                    messages=[{"role": "user", "content": message}],
                    output_config={"effort": "low"},
                )
            except Exception as e:  # noqa: BLE001 - API error or no key: review without the debate
                log.warning("debate (%s) skipped: %s", side, e)
                return None
            self._log_usage(r, "debate")
            text = next((b.text for b in r.content if b.type == "text"), "").strip()
            if r.stop_reason != "end_turn" or not text:
                return None
            out[side] = text[:1500]
        return out

    def evaluate(self, candidate: Candidate, account: dict, debate: bool = False) -> Decision:
        """Any failure (API error, refusal, bad JSON) fails closed to a rejection."""
        import anthropic

        message = self.build_user_message(candidate, account)
        if debate:
            args = self.debate(message)
            if args:
                message += "\n\ndebate:\n" + json.dumps(args, indent=2)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                # 1-hour TTL: reviews come in scan bursts 5-60 minutes apart, past the 5-minute default
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                messages=[{"role": "user", "content": message}],
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
        except Exception:  # noqa: BLE001 - e.g. no API key (the SDK raises TypeError): still a rejection
            log.exception("Claude review failed")
            return _reject("review_failed")

        self._log_usage(response, "review")
        if response.stop_reason != "end_turn":
            return _reject(f"stop_reason_{response.stop_reason}")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = DecisionSchema.model_validate_json(text)
        except ValidationError as e:
            log.error("Invalid decision JSON: %s", e)
            return _reject("invalid_json")
        return Decision(**parsed.model_dump())
