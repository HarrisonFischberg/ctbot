"""The assistant's allowlisted actions: propose -> (your click) -> apply -> audit, and revert.

The model never calls these directly. Its tool calls become *proposals*: validated here, stored in
memory under a server-issued id and shown as a card. Only POST /api/assistant/apply with that id
(your Apply click) runs one, and it is validated again first, against the state at that moment.

Actions:
  set_setting        one knob in tuning.KNOBS, inside its bounds (~/.ctbot/tuning.json)
  set_strategy_mode  shadow <-> trade in config/strategies.toml
  start_strategy     invest paper capital via the dashboard's own /api/launch logic
  stop_strategy      stop a launcher run via /api/launch/stop
  paper_order        queue a paper buy/sell via the dashboard's queue_order (manual_queue)
  approve_idea       buy a pending recommended idea via queue_order

Nothing here can touch live trading: tuning.DENYLIST is checked on every action's parameters,
whatever the action, and this module never imports ctbot.live.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ctbot import allocations, manual_queue, market_hours, tuning
from ctbot.assistant import audit
from ctbot.config import Settings

PROPOSAL_TTL_S = 15 * 60
SYMBOL = re.compile(r"[A-Z][A-Z.]{0,9}")
# The assistant may only start a strategy with a max loss, and a tighter one than the Invest form.
START_MAX_LOSS = (0.05, 0.35)
START_TAKE_PROFIT = (0.05, 1.0)

Route = Callable[[dict], tuple[int, dict]]


@dataclass
class Backend:
    """What the actions run through: the dashboard's own route functions, so the launcher caps
    and the paper order queue are never duplicated here."""
    settings: Callable[[], Settings]  # current settings, tuning applied
    launch: Route
    launch_stop: Route
    queue_order: Route
    audit_path: Path
    tuning_path: Path | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


@dataclass
class Plan:
    action: str
    params: dict[str, Any]
    summary: str
    before: Any = None
    after: Any = None
    note: str = ""
    revertible: bool = False
    undo: dict[str, Any] | None = None  # {"action", "params"} that restores `before`


def _denied_param(params: dict[str, Any]) -> str | None:
    for k, v in params.items():
        for s in (k, v):
            if isinstance(s, str) and tuning.denied(s.strip().upper()):
                return s
    return None


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise ValueError(f"{what} must be a number")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if n != n:
        raise ValueError(f"{what} must be a number")
    return n


def _strategy_table(s: Settings, name: Any) -> dict[str, Any]:
    from ctbot.strategies.registry import load_strategy_config

    cfg = load_strategy_config(s.strategies_file)
    if not isinstance(name, str) or name not in cfg:
        raise ValueError(f"unknown strategy {name!r}")
    if not cfg[name].get("enabled"):
        raise ValueError(f"{name} is disabled in strategies.toml")
    return cfg[name]


def _restart(name: str, run: dict[str, Any]) -> dict[str, Any]:
    """start_strategy params that put a launcher run back as it was (limits clamped to the
    assistant's bounds)."""
    def clamp(v, lo_hi):
        return None if v is None else min(max(float(v), lo_hi[0]), lo_hi[1])

    return {"strategy": name, "capital": run["capital"],
            "max_loss_pct": clamp(run.get("max_loss_pct") or 0.15, START_MAX_LOSS),
            "take_profit_pct": clamp(run.get("take_profit_pct"), START_TAKE_PROFIT)}


def _today_ideas(s: Settings, now: datetime) -> list[dict[str, Any]]:
    today = market_hours.trading_date(now)
    out = []
    for i in manual_queue._read_jsonl(s.ideas_path):
        try:
            if market_hours.trading_date(datetime.fromisoformat(i["ts"])) == today:
                out.append(i)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def plan(b: Backend, action: str, params: dict[str, Any], internal: bool = False) -> Plan:
    """Validate one action against the allowlist, its bounds and the current state. Raises
    ValueError with a plain reason. `internal` is only for reverts (may remove a stored knob)."""
    if not isinstance(params, dict):
        raise ValueError("parameters must be an object")
    bad = _denied_param(params)
    if bad is not None:
        raise ValueError(f"{bad} can never be changed from the dashboard assistant")
    s = b.settings()

    if action == "set_setting":
        key = params.get("key")
        if internal and params.get("value") is None:  # revert to .env / default
            if not isinstance(key, str) or tuning.denied(key) or key not in tuning.KNOBS:
                raise ValueError(f"{key} is not a setting the assistant may change")
            value = None
        else:
            value = tuning.validate(key, params.get("value"))
        before = tuning.current(s, key)
        if value is not None and value == before:
            raise ValueError(f"{tuning.KNOBS[key].label} is already {tuning.fmt(key, before)}")
        stored = key in tuning.load(b.tuning_path)
        label = tuning.KNOBS[key].label
        return Plan(action, {"key": key, "value": value},
                    f"{label} {tuning.fmt(key, before)} → {tuning.fmt(key, value) if value is not None else 'your .env value'}",
                    before=before, after=value,
                    note="Applies to the running paper bot within a few seconds, no restart.",
                    revertible=True,
                    undo={"action": "set_setting", "params": {"key": key, "value": before if stored else None}})

    if action == "set_strategy_mode":
        name, mode = params.get("strategy"), params.get("mode")
        if mode not in ("trade", "shadow"):
            raise ValueError("mode must be trade or shadow")
        before = str(_strategy_table(s, name).get("mode", "trade"))
        if before == mode:
            raise ValueError(f"{name} is already in {mode} mode")
        note = "Applies to the running paper bot within a few seconds, no restart."
        if allocations.load(s.allocations_path):
            note += (" While anything is invested through Discover, the launcher decides what trades,"
                     " so this takes effect once nothing is invested there.")
        return Plan(action, {"strategy": name, "mode": mode},
                    f"{name}: {before} → {mode}" + (" (its buys will be executed, in paper)" if mode == "trade" else
                                                    " (it only suggests ideas)"),
                    before=before, after=mode, note=note, revertible=True,
                    undo={"action": "set_strategy_mode", "params": {"strategy": name, "mode": before}})

    if action == "start_strategy":
        name = params.get("strategy")
        _strategy_table(s, name)
        cap_max = s.risk.max_trading_capital
        capital = round(_num(params.get("capital"), "capital"), 2)
        if not 10.0 <= capital <= cap_max:
            raise ValueError(f"capital must be $10-${cap_max:,.0f}")
        loss = params.get("max_loss_pct")
        loss = 0.15 if loss is None else _num(loss, "max loss")
        if not START_MAX_LOSS[0] <= loss <= START_MAX_LOSS[1]:
            raise ValueError(f"max loss must be {START_MAX_LOSS[0]:.0%}-{START_MAX_LOSS[1]:.0%}")
        tp = params.get("take_profit_pct")
        tp = None if tp in (None, 0) else _num(tp, "take profit")
        if tp is not None and not START_TAKE_PROFIT[0] <= tp <= START_TAKE_PROFIT[1]:
            raise ValueError(f"take profit must be {START_TAKE_PROFIT[0]:.0%}-{START_TAKE_PROFIT[1]:.0%}")
        prev = allocations.load(s.allocations_path).get(name)
        running = bool(prev and prev.get("status") == allocations.RUNNING)
        before = {k: prev.get(k) for k in ("status", "capital", "max_loss_pct", "take_profit_pct")} if prev else None
        summary = (f"{name} paper capital ${float(prev['capital']):,.0f} → ${capital:,.0f}" if running
                   else f"Invest ${capital:,.0f} of paper money in {name}")
        summary += f" (max loss {loss:.0%}" + (f", take profit {tp:.0%})" if tp else ")")
        undo = ({"action": "start_strategy", "params": _restart(name, prev)}
                if running else {"action": "stop_strategy", "params": {"strategy": name}})
        return Plan(action, {"strategy": name, "capital": capital, "max_loss_pct": loss, "take_profit_pct": tp},
                    summary, before=before, after={"status": allocations.RUNNING, "capital": capital},
                    note="It trades on its own with that much paper money, never more; every other model only "
                         "suggests ideas while anything is invested. Buying power is checked when you apply.",
                    revertible=True, undo=undo)

    if action == "stop_strategy":
        name = params.get("strategy")
        prev = allocations.load(s.allocations_path).get(name) if isinstance(name, str) else None
        if not prev or prev.get("status") != allocations.RUNNING:
            raise ValueError(f"{name} is not running in the launcher")
        return Plan(action, {"strategy": name}, f"Stop {name} and sell what it holds (paper)",
                    before={k: prev.get(k) for k in ("status", "capital", "max_loss_pct", "take_profit_pct")},
                    after={"status": allocations.STOPPING},
                    note="It sells at the next market check. Revert starts it again; what it sold is not bought back.",
                    revertible=True,
                    undo={"action": "start_strategy", "params": _restart(name, prev)})

    if action == "paper_order":
        side = params.get("side")
        symbol = str(params.get("symbol", "")).strip().upper()
        if side not in ("buy", "sell") or not SYMBOL.fullmatch(symbol):
            raise ValueError("side must be buy or sell, with a valid symbol")
        body: dict[str, Any] = {"side": side, "symbol": symbol}
        if side == "buy":
            notional = round(_num(params.get("notional"), "notional"), 2)
            if not 1.0 <= notional <= s.risk.max_trading_capital:
                raise ValueError(f"notional must be $1-${s.risk.max_trading_capital:,.0f}")
            body["notional"] = notional
            summary = f"Paper buy ${notional:,.2f} of {symbol}"
        else:
            from ctbot.dashboard.state import _read_json

            held = {p.get("symbol") for p in _read_json(s.paper_ledger_path).get("positions", [])}
            if symbol not in held:
                raise ValueError(f"no paper position in {symbol} to sell")
            summary = f"Paper sell all of {symbol}"
        return Plan(action, body, summary,
                    note="Queued for the paper bot, which re-runs every risk check before it fills. "
                         "A click expires after 10 minutes. An order can't be reverted; sell from Home instead.")

    if action == "approve_idea":
        iid = params.get("idea_id")
        idea = next((i for i in _today_ideas(s, b.now()) if i.get("id") == iid), None) if isinstance(iid, str) else None
        if idea is None:
            raise ValueError("unknown idea, or not from today")
        if idea.get("status") != "recommended" or idea.get("direction") != "long":
            raise ValueError(f"{idea.get('symbol')} is not waiting for your approval (status {idea.get('status')})")
        if any(o.get("idea_id") == iid for o in manual_queue.read_orders(s.manual_orders_path)):
            raise ValueError(f"{idea.get('symbol')} was already clicked")
        d, rk = idea.get("decision") or {}, idea.get("risk") or {}
        notional = rk.get("notional") or d.get("suggested_notional") or None
        if notional is not None:
            notional = round(min(float(notional), s.risk.max_trading_capital), 2)
        body = {"side": "buy", "symbol": idea["symbol"], "idea_id": iid}
        if notional:
            body["notional"] = notional
        return Plan(action, body,
                    f"Approve the recommended paper buy of {idea['symbol']}" + (f" (${notional:,.2f})" if notional else ""),
                    note="Same as pressing Buy on the idea: the paper bot re-runs every risk check before it fills. "
                         "An order can't be reverted.")

    raise ValueError(f"{action!r} is not an action the assistant may take")


def execute(b: Backend, p: Plan) -> Any:
    """Run a validated plan. Returns the value after. Raises ValueError on a refused change."""
    s = b.settings()
    if p.action == "set_setting":
        key, value = p.params["key"], p.params["value"]
        tuning.save(key, value, b.tuning_path)
        if value is None:
            tuning._env_fallback(key)
        else:
            os.environ[key] = str(value)
        return tuning.current(b.settings(), key)
    if p.action == "set_strategy_mode":
        from ctbot.strategies.registry import set_mode

        set_mode(s.strategies_file, p.params["strategy"], p.params["mode"])
        return p.params["mode"]
    route = {"start_strategy": b.launch, "stop_strategy": b.launch_stop,
             "paper_order": b.queue_order, "approve_idea": b.queue_order}[p.action]
    status, payload = route(dict(p.params))
    if status >= 300:
        raise ValueError(payload.get("error") or f"refused ({status})")
    if p.action == "start_strategy":
        return {"status": allocations.RUNNING, "capital": payload["run"]["capital"]}
    if p.action == "stop_strategy":
        return {"status": allocations.STOPPING}
    return {"queued": payload.get("queued")}


def card(p: Plan, pid: str, reason: str, risk: str) -> dict[str, Any]:
    return {"id": pid, "action": p.action, "summary": p.summary, "reason": reason, "risk": risk,
            "note": p.note, "before": p.before, "after": p.after, "revertible": p.revertible}


@dataclass
class Proposals:
    """Pending proposals, held in the dashboard process under server-issued ids."""
    backend: Backend
    ttl_s: float = PROPOSAL_TTL_S
    clock: Callable[[], float] = time.monotonic
    _items: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def propose(self, action: str, params: dict[str, Any], reason: str = "", risk: str = "",
                user_message: str = "", conversation_id: str = "") -> dict[str, Any]:
        """A card for the UI, or {"rejected": reason}. Never changes anything but the audit log."""
        reason, risk = str(reason)[:600], str(risk)[:400]
        try:
            p = plan(self.backend, action, params)
        except ValueError as e:
            audit.record(self.backend.audit_path, "rejected", action=action, params=params, error=str(e),
                         user_message=user_message, conversation_id=conversation_id, reason=reason)
            return {"rejected": str(e)}
        pid = uuid4().hex
        with self._lock:
            self._expire()
            self._items[pid] = {"action": action, "params": p.params, "reason": reason, "risk": risk,
                                "user_message": user_message, "conversation_id": conversation_id,
                                "created": self.clock()}
        audit.record(self.backend.audit_path, "proposed", proposal_id=pid, action=action, params=p.params,
                     before=p.before, after=p.after, summary=p.summary, reason=reason, risk=risk,
                     user_message=user_message, conversation_id=conversation_id)
        return card(p, pid, reason, risk)

    def _expire(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._items.items() if now - v["created"] > self.ttl_s]:
            del self._items[k]

    def _take(self, pid: Any) -> dict[str, Any]:
        with self._lock:
            self._expire()
            item = self._items.pop(pid, None) if isinstance(pid, str) else None
        if item is None:
            raise KeyError("that suggestion has expired or was already used; ask again")
        return item

    def drop(self, pids: list[str]) -> None:
        with self._lock:
            for pid in pids:
                self._items.pop(pid, None)

    def apply(self, pid: Any) -> dict[str, Any]:
        """Your Apply click. Re-validates, runs, audits. KeyError for an unknown/expired id."""
        item = self._take(pid)
        meta = {k: item[k] for k in ("user_message", "conversation_id", "reason", "risk")}
        return self._run(item["action"], item["params"], proposal_id=pid, **meta)

    def dismiss(self, pid: Any) -> dict[str, Any]:
        item = self._take(pid)
        audit.record(self.backend.audit_path, "dismissed", proposal_id=pid, action=item["action"],
                     params=item["params"], user_message=item["user_message"],
                     conversation_id=item["conversation_id"])
        return {"dismissed": pid}

    def revert(self, audit_id: Any) -> dict[str, Any]:
        """Your Revert click on an applied change: restores its logged before-value."""
        with self._lock:  # one revert at a time, so a double click can't undo twice
            path = self.backend.audit_path
            entry = audit.find(path, audit_id) if isinstance(audit_id, str) else None
            if entry is None or entry.get("event") != "applied":
                raise KeyError("no applied change with that id")
            if not entry.get("revertible") or not entry.get("undo"):
                raise ValueError("that change can't be reverted")
            if audit_id in audit.reverted_ids(audit.read(path)):
                raise ValueError("that change was already reverted")
            undo = entry["undo"]
            return self._run(undo["action"], undo["params"], internal=True, revert_of=audit_id,
                             user_message="(revert)", conversation_id=entry.get("conversation_id", ""))

    def _run(self, action: str, params: dict[str, Any], internal: bool = False, **meta: Any) -> dict[str, Any]:
        path = self.backend.audit_path
        try:
            p = plan(self.backend, action, params, internal=internal)
            after = execute(self.backend, p)
        except (ValueError, KeyError, OSError) as e:
            entry = audit.record(path, "failed", action=action, params=params, error=str(e), **meta)
            return {"ok": False, "error": str(e), "audit_id": entry["id"]}
        entry = audit.record(path, "applied", action=action, params=p.params, summary=p.summary,
                             before=p.before, after=after, revertible=p.revertible, undo=p.undo, **meta)
        return {"ok": True, "summary": p.summary, "before": p.before, "after": after, "note": p.note,
                "audit_id": entry["id"], "revertible": p.revertible}
