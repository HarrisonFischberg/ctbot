"""Claude review: usage logging, the optional bounded debate, outcome memory (all offline)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from ctbot import llm_costs
from ctbot.decision.engine import ClaudeDecisionEngine
from ctbot.decision.memory import outcome_memory
from ctbot.models import Candidate, Direction, Signal

VERDICT = json.dumps({"approve": True, "confidence": 0.7, "rationale": "ok", "suggested_notional": 40.0,
                      "stop_loss_pct": None, "take_profit_pct": None})


class _Client:
    def __init__(self, fail_debate=False):
        self.calls, self.fail_debate = [], fail_debate
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        is_debate = "output_config" in kw and "format" not in kw["output_config"]
        if is_debate and self.fail_debate:
            import anthropic
            import httpx

            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        text = "argument" if is_debate else VERDICT
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=500,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


CAND = Candidate("KO", Direction.LONG, 0.5, (Signal("rsi2_etf", "KO", Direction.LONG, 1.0, 5),))


def test_review_logs_usage_and_cost(tmp_path):
    path = tmp_path / "u.jsonl"
    eng = ClaudeDecisionEngine("claude-opus-5-5", client=_Client(), usage_path=path)
    assert eng.evaluate(CAND, {}).approve
    assert eng.client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    row = json.loads(path.read_text())
    assert row["purpose"] == "review" and abs(row["usd"] - (3000 * 4 + 500 * 20) / 1e6) < 1e-9
    from datetime import date

    assert llm_costs.spent(path, date.fromisoformat(row["day"])) == row["usd"]
    assert llm_costs.summary(path)["by_purpose"] == {"review": row["usd"]}


def test_one_hour_cache_writes_cost_double():
    u = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
         "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
    assert llm_costs.cost_usd("claude-opus-5-5", u) == 8.0  # 2x the $4 input price
    assert llm_costs.cost_usd("claude-opus-5-5", {**u, "cache_creation": None}) == 5.0  # 5-minute: 1.25x


def test_debate_adds_two_low_effort_calls_and_never_blocks(tmp_path):
    c = _Client()
    eng = ClaudeDecisionEngine("claude-haiku-5-5", client=c, usage_path=tmp_path / "u.jsonl")
    assert eng.evaluate(CAND, {}, debate=True).approve
    assert len(c.calls) == 3 and "debate:" in c.calls[-1]["messages"][0]["content"]
    assert all(k["output_config"] == {"effort": "low"} for k in c.calls[:2])
    c2 = _Client(fail_debate=True)
    assert ClaudeDecisionEngine("m", client=c2).evaluate(CAND, {}, debate=True).approve
    assert "debate:" not in c2.calls[-1]["messages"][0]["content"]


def test_outcome_memory_is_as_of(tmp_path):
    ledger = tmp_path / "l.json"
    fills = [
        {"symbol": "KO", "side": "buy", "quantity": 1, "price": 50, "ts": "2026-10-01T15:00:00+00:00", "tag": "rsi2_etf"},
        {"symbol": "KO", "side": "sell", "quantity": 1, "price": 55, "ts": "2026-10-03T15:00:00+00:00", "tag": "time_exit"},
        {"symbol": "KO", "side": "buy", "quantity": 1, "price": 55, "ts": "2026-10-05T15:00:00+00:00", "tag": "rsi2_etf"},
        {"symbol": "KO", "side": "sell", "quantity": 1, "price": 44, "ts": "2026-10-08T15:00:00+00:00", "tag": "stop_loss"},
    ]
    ledger.write_text(json.dumps({"fills": fills}))
    early = outcome_memory(ledger, ["rsi2_etf"], "KO", datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert early["models"]["rsi2_etf"]["closed_trades"] == 1 and early["models"]["rsi2_etf"]["win_rate"] == 1.0
    late = outcome_memory(ledger, ["rsi2_etf"], "KO", datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert late["models"]["rsi2_etf"]["closed_trades"] == 2 and late["models"]["rsi2_etf"]["win_rate"] == 0.5
    assert [t["exit"] for t in late["symbol_last_trades"]] == ["time_exit", "stop_loss"]
