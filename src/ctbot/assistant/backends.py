"""Bub's model backends, tried in order: Claude first, OpenAI as the fallback.

- anthropic: ANTHROPIC_API_KEY, the same lazy anthropic.Anthropic() client as the decision engine.
- openai: OPENAI_API_KEY, Chat Completions over httpx (no extra dependency).

Each client exposes `.messages.create(...)` with Anthropic's arguments and returns Anthropic's
response shape, so chat.py runs the same tool loop on any of them.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import httpx

from ctbot.config import Settings

OPENAI_URL = "https://api.openai.com/v1"
TIMEOUT_S = 90.0
LABELS = {"anthropic": "Claude", "openai": "OpenAI"}
ORDER = ("anthropic", "openai")


def configured(settings: Settings) -> list[str]:
    """The backends, in ORDER, whose API key is set."""
    keys = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
    return [b for b in ORDER if os.environ.get(keys[b], "").strip()]


def model_for(name: str, settings: Settings) -> str:
    return settings.openai_model if name == "openai" else settings.claude_model


def make(name: str, settings: Settings):
    if name == "anthropic":
        import anthropic

        return anthropic.Anthropic()
    if name == "openai":
        return OpenAICompat(OPENAI_URL, os.environ.get("OPENAI_API_KEY", "").strip())
    raise ValueError(f"unknown assistant backend {name}")


class OpenAICompat:
    """OpenAI's /chat/completions behind Anthropic's messages.create shape."""

    def __init__(self, base_url: str, key: str, http: httpx.Client | None = None) -> None:
        self.base_url, self.key = base_url, key
        self.http = http or httpx.Client(timeout=TIMEOUT_S)
        self.messages = self

    def create(self, *, model: str, system: list[dict[str, Any]], messages: list[dict[str, Any]],
               tools: list[dict[str, Any]], **_ignored: Any) -> SimpleNamespace:
        body = {"model": model, "messages": to_openai(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        return from_openai(r.json())


def to_openai(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": "\n\n".join(b["text"] for b in system)}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in c if b["type"] == "tool_use"]
            out.append({"role": "assistant", "content": "".join(b["text"] for b in c if b["type"] == "text") or None,
                        **({"tool_calls": calls} if calls else {})})
        else:  # tool results go back one message per call
            out += [{"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                    for b in c if b["type"] == "tool_result"]
    return out


def from_openai(data: dict[str, Any]) -> SimpleNamespace:
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    content = [SimpleNamespace(type="text", text=msg["content"])] if msg.get("content") else []
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content.append(SimpleNamespace(type="tool_use", id=tc["id"], name=tc["function"]["name"], input=args))
    stop = "tool_use" if any(b.type == "tool_use" for b in content) else (
        "max_tokens" if choice.get("finish_reason") == "length" else "end_turn")
    return SimpleNamespace(stop_reason=stop, content=content)
