"""One cycle: alt data -> strategies -> ensemble -> Claude -> risk -> execute.

The executor is injected by the entrypoint. This module must not import ctbot.live.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ctbot import (
    allocations,
    autopilot,
    llm_costs,
    market_hours,
    risk_mode,
    sectors,
    thoughts,
    track_record,
    trade_log,
)
from ctbot.aggregation.ensemble import combine
from ctbot.config import Settings
from ctbot.contracts import (
    CycleSummary,
    ExecutionOutcome,
    MarketSnapshot,
    ProposedOrder,
    StrategyResult,
    strategy_version,
)
from ctbot.decision.engine import ClaudeDecisionEngine
from ctbot.decision.memory import outcome_memory
from ctbot.execution.base import OrderExecutor
from ctbot.market_data.base import MarketDataProvider
from ctbot.models import Candidate, Decision, Direction, OrderRequest, Quote, Side, Signal
from ctbot.providers.base import AltDataProvider
from ctbot.providers.registry import fetch_all
from ctbot.risk.manager import AccountState, RiskManager, RiskVerdict
from ctbot.strategies.base import StrategyContext
from ctbot.strategies.registry import WeightedStrategy, required_event_kinds

log = logging.getLogger(__name__)

EVENT_LOOKBACK_DAYS = 90


@dataclass
class Components:
    settings: Settings
    market_data: MarketDataProvider
    providers: list[AltDataProvider]
    strategies: list[WeightedStrategy]
    engine: ClaudeDecisionEngine
    risk: RiskManager
    executor: OrderExecutor
    universe: list[str]
    # Receives one dict per evaluated candidate ("trade idea"); the paper entrypoint
    # appends them to a JSONL file the dashboard reads.
    recorder: Callable[[dict[str, Any]], None] = field(default=lambda idea: None)
    # (trading day, symbol, strategies) already sent to Claude: re-sending the same setup
    # every cycle would only multiply API cost.
    reviewed: set = field(default_factory=set)
    # Optional MarketContext: time, index moves, earnings and analyst data for reviews,
    # and the earnings guard.
    context: Any = None
    reviews_today: dict = field(default_factory=dict)  # trading day -> Claude reviews used
    # strategies as configured in strategies.toml; `strategies` is this, as the launcher sees it
    configured: list[WeightedStrategy] | None = None
    launched: tuple | None = None  # last applied launcher state: (in use, {name: capital})
    # (symbol, side) -> monotonic time of the last submission: refuses an identical order within
    # DUPLICATE_WINDOW_S (a retried cycle, a double click, a strategy firing twice)
    recent_orders: dict = field(default_factory=dict)
    journal: Any = None  # ctbot.contracts.Journal: snapshot -> results -> order -> outcome
    fills: int = 0       # fills submitted by this process (cycle summaries count the difference)
    trend: Any = field(default_factory=risk_mode.TrendCache)  # pull-back trend average, once a day
    think: bool = True  # Bub's thoughts (ctbot.thoughts): notes only, never changes a trade


DUPLICATE_WINDOW_S = 120.0


def _journal(c: Components, record) -> None:
    if c.journal is not None:
        c.journal.write(record)


def halted(c: Components) -> bool:
    """Kill-switch file (data/HALT next to the paper ledger): no new entries while it exists."""
    return c.settings.halt_path.exists()


def apply_launcher(c: Components, state: AccountState, positions: dict, now: datetime) -> None:
    """Paper launcher (ctbot.allocations). Running strategies trade alone, sized off their own
    capital; every other model is shadow. Stopping strategies sell what they opened. Once you
    have invested through the launcher it stays in charge, so a stopped strategy stays stopped;
    an empty data/allocations.json hands control back to strategies.toml."""
    if c.configured is None:
        c.configured = list(c.strategies)
    ap = autopilot.load(c.settings.autopilot_path)
    if ap.enabled and not c.executor.is_live:  # paper only: replaces the launcher
        try:
            fills = json.loads(c.settings.paper_ledger_path.read_text()).get("fills", [])
        except (OSError, ValueError):
            fills = []
        autopilot.apply(c, state, ap, fills, now)
        return
    c.risk.max_loss_pct = c.risk.trail_giveback = c.risk.min_confidence = None
    path = c.settings.allocations_path
    used: dict[str, float] = {}
    open_gain: dict[str, float] = {}
    for sym, p in positions.items():  # what each strategy holds now (at cost) and its open gain
        owner = (state.brackets.get(sym) or {}).get("model")
        if owner:
            used[owner] = used.get(owner, 0.0) + p.quantity * p.avg_cost
            q = c.market_data.quotes([sym]).get(sym)
            mark = q.mid if q and q.mid else p.avg_cost
            open_gain[owner] = open_gain.get(owner, 0.0) + (mark - p.avg_cost) * p.quantity
    for name, run in allocations.load(path).items():  # max loss / take profit, before anything trades
        if run.get("status") != allocations.RUNNING:
            continue
        gain = float(run.get("realized", 0.0)) + open_gain.get(name, 0.0)
        cap = float(run["capital"])
        if run.get("max_loss_pct") and gain <= -run["max_loss_pct"] * cap:
            allocations.stop(path, name, f"max loss reached ({gain:+.2f})")
            log.warning("launcher: %s hit its max loss (%+.2f on $%.0f): stopping and selling", name, gain, cap)
        elif run.get("take_profit_pct") and gain >= run["take_profit_pct"] * cap:
            allocations.stop(path, name, f"profit target reached ({gain:+.2f})")
            log.info("launcher: %s hit its take-profit (%+.2f on $%.0f): stopping and selling", name, gain, cap)

    in_use = bool(allocations.load(path))
    runs = allocations.running(path)
    if (in_use, runs) != c.launched:
        c.strategies = [
            replace(ws, mode="trade", standalone=True) if ws.strategy.name in runs else replace(ws, mode="shadow")
            for ws in c.configured
        ] if in_use else list(c.configured)
        c.launched = (in_use, runs)
        log.info("launcher: %s", ", ".join(f"{k} ${v:,.0f}" for k, v in runs.items())
                 or ("nothing invested: every strategy only suggests ideas" if in_use else "off (strategies.toml)"))
    state.sleeves = dict(runs)
    state.sleeve_used = {n: used.get(n, 0.0) for n in runs}
    # each strategy's unspent money is kept for it: no other strategy may buy with it
    state.sleeve_reserve = {n: max(0.0, cap - used.get(n, 0.0)) for n, cap in runs.items()}

    today = market_hours.trading_date(now)
    for name in allocations.stopping(c.settings.allocations_path):
        owned = [s for s in positions if (state.brackets.get(s) or {}).get("model") == name]
        for sym in owned:
            quote = c.market_data.quotes([sym]).get(sym)
            sig = Signal(name, sym, Direction.EXIT, 1.0, 0, {"reason": "stopped_on_dashboard"})
            verdict = c.risk.check_exit(Candidate(sym, Direction.EXIT, -1.0, (sig,)), positions[sym], state, quote, today)
            if verdict.allowed:
                for order in verdict.orders:
                    if _submit(c, state, order, positions, today, tag="stopped"):
                        log.info("launcher: sold %s for stopped strategy %s", sym, name)
        positions.clear()
        positions.update({p.symbol: p for p in c.executor.positions()})
        if not any((state.brackets.get(s) or {}).get("model") == name for s in positions):
            allocations.mark_stopped(c.settings.allocations_path, name)
            log.info("launcher: %s stopped", name)


def _book_sleeve_gain(c: Components, state: AccountState, symbol: str, avg_cost: float, fill) -> None:
    """Launcher: add a sale's gain/loss to its strategy's run (for max loss / take profit)."""
    owner = (state.brackets.get(symbol) or {}).get("model")
    if owner in state.sleeves or owner in allocations.stopping(c.settings.allocations_path):
        allocations.add_realized(c.settings.allocations_path, owner, (fill.price - avg_cost) * fill.quantity)


def _sleeve_signals(ws: WeightedStrategy, sigs: list[Signal], sleeves: dict[str, float]) -> list[Signal]:
    """A launched core (a model with size_pct, e.g. the trend core) fills its whole capital:
    its size_pct, set as a share of the full book, becomes a share of its own capital."""
    base = getattr(ws.strategy, "size_pct", None)
    if ws.strategy.name not in sleeves or not base or not hasattr(ws.strategy, "target_weight"):
        return sigs
    return [replace(s, evidence={**s.evidence, "size_pct": round(min(1.0, s.evidence["size_pct"] / base), 4)})
            if "size_pct" in s.evidence else s for s in sigs]


def _idea(
    now: datetime, cand: Candidate, quote: Quote | None, decision: Decision | None, verdict, status: str
) -> dict[str, Any]:
    return {
        "id": uuid4().hex,
        "ts": now.isoformat(),
        "symbol": cand.symbol,
        "direction": cand.direction.value,
        "score": cand.score,
        "strategies": list(cand.strategies),
        "signals": [
            {"strategy": s.strategy, "direction": s.direction.value, "strength": round(s.strength, 3),
             "horizon_days": s.horizon_days, "evidence": s.evidence}
            for s in cand.signals
        ],
        "quote": {"bid": quote.bid, "ask": quote.ask, "last": quote.last} if quote else None,
        "decision": None if decision is None else {
            "approve": decision.approve, "confidence": decision.confidence,
            "rationale": decision.rationale, "suggested_notional": decision.suggested_notional,
            "stop_loss_pct": decision.stop_loss_pct, "take_profit_pct": decision.take_profit_pct,
        },
        "risk": None if verdict is None else {
            "allowed": verdict.allowed, "reasons": list(verdict.reasons),
            "notional": verdict.orders[0].notional if verdict.orders else None,
        },
        "status": status,
    }


def _earnings_block(c: Components, cand: Candidate, now: datetime) -> datetime | None:
    """The earnings report that falls before this long's planned exit (the hard rule refuses it)."""
    report = c.context.next_earnings(cand.symbol) if c.context else None
    if report is None:
        return None
    plan = c.risk.exit_plan(cand)
    horizon = max((s.horizon_days for s in cand.signals if s.direction is Direction.LONG), default=1.0)
    planned_exit = datetime.fromisoformat(plan["exit_at"]) if plan else now + timedelta(days=horizon)
    return report if now < report < planned_exit else None


def live_quotes(md: MarketDataProvider) -> bool:
    return getattr(md, "quotes_are_live", True)


def _entry_gates(c: Components, cand: Candidate) -> dict[str, Any]:
    """Today's session VWAP and SPY's regime label, fetched only when their gate is on."""
    L, out = c.settings.risk, {}
    if L.vwap_entry_gate and hasattr(c.market_data, "today_bars"):
        from ctbot.research.backtest import vwap_series

        try:
            bars = c.market_data.today_bars([cand.symbol]).get(cand.symbol) or []
            if bars:
                out["vwap"] = vwap_series(bars)[-1]
        except Exception:  # noqa: BLE001 - no bars means no gate, never a blocked cycle
            log.exception("vwap unavailable for %s", cand.symbol)
    if L.regime_gate and c.context and hasattr(c.context, "market_regime"):
        out["market_regime"] = (c.context.market_regime() or {}).get("label")
    return out


def avg_dollar_volume(md: MarketDataProvider, symbol: str, days: int = 20) -> float:
    bars = md.daily_bars(symbol, days)
    return sum(b.close * b.volume for b in bars) / len(bars) if bars else 0.0


def _submit(c: Components, state: AccountState, order, positions, today, decision=None,
            plan=None, tag: str | None = None) -> bool:
    if tag:
        order = replace(order, candidate_ref=tag)
    _journal(c, ProposedOrder(order.client_order_id, order.symbol, order.side.value, order.notional,
                              order.limit_price, tag))
    if order.side is Side.BUY and halted(c):
        log.warning("refused %s %s: HALT file present (%s)", order.side.value, order.symbol, c.settings.halt_path)
        _journal(c, ExecutionOutcome(order.client_order_id, order.symbol, "refused", reason="halt_file"))
        return False
    key, t = (order.symbol, order.side), time.monotonic()
    if order.side is Side.BUY and key in c.recent_orders and t - c.recent_orders[key] < DUPLICATE_WINDOW_S:
        log.warning("refused duplicate %s %s within %.0fs", order.side.value, order.symbol, DUPLICATE_WINDOW_S)
        _journal(c, ExecutionOutcome(order.client_order_id, order.symbol, "refused", reason="duplicate_order"))
        return False
    fill = c.executor.submit(order)
    log.info("submitted %s live=%s fill=%s", order, c.executor.is_live, fill)
    if fill is None:
        _journal(c, ExecutionOutcome(order.client_order_id, order.symbol, "no_fill"))
        return False
    c.fills += 1
    if order.side is Side.BUY:  # exits are never blocked, and an order that didn't fill may retry
        c.recent_orders[key] = t
    _journal(c, ExecutionOutcome(order.client_order_id, order.symbol, "filled", fill.quantity, fill.price,
                                 simulated=fill.simulated))
    if order.side is Side.SELL:
        pos = positions[order.symbol]
        bracket = dict(state.brackets.get(order.symbol) or {})
        _book_sleeve_gain(c, state, order.symbol, pos.avg_cost, fill)
        if tag == "partial_profit" and any(p.symbol == order.symbol for p in c.executor.positions()):
            c.risk.partial_taken(state, order.symbol, pos.avg_cost)  # the rest keeps its bracket
        else:
            c.risk.record_exit_fill(state, pos, today, fill.price)
        try:
            trade_log.append(c.settings.trade_log_path, trade_log.close_record(
                order.symbol, bracket, pos.avg_cost, fill.quantity, fill.price, pos.opened_at,
                datetime.now(timezone.utc), tag))
        except Exception:  # noqa: BLE001 - the post-mortem never blocks trading
            log.exception("trade log failed for %s", order.symbol)
    else:
        c.risk.set_bracket(state, order.symbol, fill.price, decision, plan)
        bracket = state.brackets.get(order.symbol)
        state.entry_dates.append(today)
        if bracket is not None:
            state.open_risk += fill.quantity * max(0.0, fill.price - bracket["stop"])
        sec = sectors.sector(order.symbol)
        state.sector_exposure[sec] = (state.sector_exposure.get(sec, 0.0)
                                      + fill.quantity * fill.price * sectors.LEVERAGE.get(order.symbol, 1.0))
        if bracket is not None:
            models = [m for m in (tag or "").split(",") if m]
            try:
                bracket["ctx"] = trade_log.entry_context(
                    c.market_data, order.symbol, fill.price, datetime.now(timezone.utc), models, decision,
                    track_record.for_candidate(c.risk.size_mult, models), sec, state.risk_off,
                    c.settings.risk.risk_off_trend_symbol, c.trend.avg)
                bracket["ctx"]["r0"] = round(fill.price - bracket["stop"], 4)  # risk a share at entry: 1R
            except Exception:  # noqa: BLE001
                log.exception("trade log entry context failed for %s", order.symbol)
        launched = [m for m in (tag or "").split(",") if m in state.sleeves]
        if bracket is not None and not bracket.get("model") and launched:
            bracket["model"] = launched[0]  # launcher: the position counts against that strategy
        owner = (bracket or {}).get("model")
        if owner in state.sleeves:  # its spent money is no longer held back for it this cycle
            state.sleeve_used[owner] = state.sleeve_used.get(owner, 0.0) + fill.quantity * fill.price
            state.sleeve_reserve[owner] = max(0.0, state.sleeves[owner] - state.sleeve_used[owner])
    sync_cash(c.executor, state)
    state.open_positions = len(c.executor.positions())
    return True


def sync_cash(executor, state: AccountState) -> None:
    """Cash, plus buying power and the maintenance flag when the paper broker runs on margin."""
    state.cash = executor.cash()
    if getattr(executor, "margin", 1.0) > 1.0:
        state.buying_power, state.margin_call = executor.margin_status()
    else:
        state.buying_power, state.margin_call = None, False


def _maybe_top_up(c: Components, state: AccountState, cand: Candidate, pos, quote, now: datetime,
                  buys_ok: bool = True) -> None:
    """Systematic core only: rebalance to its target size when it is off by more than 5% of
    equity. Buys respect the kill switch, the daily-loss halt, HALT and the close cutoff; trims (partial sells, used
    by the volatility-capped core) always may run and keep the position's exit plan."""
    sig = next((s for s in cand.signals if s.direction is Direction.LONG and s.evidence.get("rebalance")), None)
    owner = (state.brackets.get(cand.symbol) or {}).get("model")
    if sig is None or owner != sig.strategy or quote is None or not quote.ask:
        return
    if not any(ws.strategy.name == owner and ws.mode == "trade" for ws in c.strategies):
        return  # shadow (e.g. another strategy is running in the launcher): leave the size alone
    L = c.settings.risk
    from ctbot.models import OrderRequest

    sleeve = state.sleeves.get(owner)
    base = sleeve or min(state.equity, L.max_trading_capital)
    target = base * min(sig.evidence["size_pct"], 1.0 if sleeve else L.max_core_pct)
    band = 0.05 * (sleeve or state.equity)
    gap = target - pos.quantity * quote.mid
    if gap < -band and quote.bid:
        trim = OrderRequest(cand.symbol, Side.SELL, round(-gap, 2), limit_price=quote.bid, candidate_ref="rebalance")
        fill = c.executor.submit(trim)
        if fill:  # partial: bracket and day-trade count untouched
            _book_sleeve_gain(c, state, cand.symbol, pos.avg_cost, fill)
            log.info("trim %s: -$%.2f to its target $%.0f", cand.symbol, -gap, target)
            sync_cash(c.executor, state)
        return
    if not buys_ok or state.kill_switch or state.margin_call or state.equity <= state.day_start_equity * (1 - L.max_daily_loss_pct):
        return
    spendable = state.cash if state.buying_power is None or state.risk_off else state.buying_power
    cash = spendable - sum(v for k, v in state.sleeve_reserve.items() if k != owner)  # others' money stays theirs
    if gap <= band or cash < 1:
        return

    order = OrderRequest(cand.symbol, Side.BUY, round(min(gap, cash), 2), limit_price=quote.ask,
                         candidate_ref=sig.strategy)
    fill = c.executor.submit(order)
    if fill:
        if sleeve:
            state.sleeve_used[owner] = state.sleeve_used.get(owner, 0.0) + fill.quantity * fill.price
            state.sleeve_reserve[owner] = max(0.0, sleeve - state.sleeve_used[owner])
        log.info("top-up %s: +$%.2f to its target $%.0f", cand.symbol, order.notional, target)
        sync_cash(c.executor, state)


def refresh_learning(c: Components, state: AccountState, now: datetime) -> None:
    """Once a cycle: each strategy's size multiplier from its closed trades, whether the account
    is pulled back after losses, and what each sector holds now (for the sector cap)."""
    L = c.settings.risk
    if L.track_record_sizing:
        try:
            fills = json.loads(c.settings.paper_ledger_path.read_text()).get("fills", [])
        except (OSError, ValueError):
            fills = []
        c.risk.size_mult = {m: v["mult"] for m, v in track_record.size_multipliers(
            fills, now, L.track_record_prior_trades, L.track_record_min_mult, L.track_record_max_mult).items()}
    else:
        c.risk.size_mult = {}

    positions = c.executor.positions()
    syms = sorted({p.symbol for p in positions} | ({L.risk_off_trend_symbol} if L.risk_off_trend_days > 0 else set()))
    try:
        quotes = c.market_data.quotes(syms) if syms else {}
    except Exception:  # noqa: BLE001 - no prices: sector exposure at cost, no trend trigger
        log.exception("quotes for the pull-back check failed")
        quotes = {}
    marks = {}
    state.open_risk = 0.0
    for p in positions:
        q = quotes.get(p.symbol)
        px = q.mid if q and q.mid else p.avg_cost
        marks[p.symbol] = p.quantity * px
        stop = (state.brackets.get(p.symbol) or {}).get("stop")
        if stop is not None:
            state.open_risk += p.quantity * max(0.0, px - stop)
    state.sector_exposure = sectors.exposure(marks)

    trend_q = quotes.get(L.risk_off_trend_symbol)
    trend_avg = c.trend.get(c.market_data, L.risk_off_trend_symbol, L.risk_off_trend_days,
                            market_hours.trading_date(now)) if L.risk_off_trend_days > 0 else None
    why = risk_mode.reasons(L, state.equity, state.day_start_equity, state.peak_equity,
                            trend_q.last if trend_q else None, trend_avg)
    if why != state.risk_off:
        if why:
            log.warning("pulled back (buys spend cash only, no borrowing): %s", "; ".join(why))
        else:
            log.info("full risk back on: no pull-back trigger holds")
    state.risk_off = why


def manage_positions(c: Components, state: AccountState, now: datetime) -> None:
    """Stops, targets, exit times and swings for open positions. Runs at the start of every
    cycle and, from the paper loop, every ~30 s in between (tight stops need it). Bypasses
    strategies, the ensemble and Claude."""
    if c.settings.respect_market_hours and not market_hours.session_open(now):
        return
    today = market_hours.trading_date(now)
    positions = {p.symbol: p for p in c.executor.positions()}
    for sym, pos in list(positions.items()):
        quote = c.market_data.quotes([sym]).get(sym)
        if quote:
            trade_log.track(state.brackets.get(sym), quote.bid)
        if quote and c.risk.trail(state, sym, quote.bid):
            b = state.brackets[sym]
            log.info("trail %s: new high %.2f, stop raised to %.2f", sym, b["peak"], b["stop"])
        report = c.context.next_earnings(sym) if c.context else None
        if quote and c.risk.maybe_swing(state, pos, quote, now, report):
            b = state.brackets[sym]
            log.info("swing %s: up %.2f%%, holding to %s with stop raised to %.2f (swing %d)", sym,
                     (quote.bid / pos.avg_cost - 1) * 100, b["exit_at"], b["stop"], b["swings"])
        half = c.risk.partial_due(state, pos, quote) if quote else 0.0
        if half and not (market_hours.trading_date(pos.opened_at) == today and c.risk.pdt_limit_hit(state, today)):
            log.info("partial_profit %s: selling half at %.2f, stop to break-even", sym, quote.bid)
            order = OrderRequest(sym, Side.SELL, round(half * quote.bid, 2), quote.bid)
            if _submit(c, state, order, positions, today, tag="partial_profit"):
                positions = {p.symbol: p for p in c.executor.positions()}
                pos = positions.get(sym)
                if pos is None:
                    continue
        reason = c.risk.bracket_hit(state, pos, quote, now) if quote else None
        if reason is None:
            continue
        sig = Signal("risk", sym, Direction.EXIT, 1.0, 0, {"reason": reason})
        cand = Candidate(sym, Direction.EXIT, -1.0, (sig,))
        verdict = c.risk.check_exit(cand, pos, state, quote, today)
        if not verdict.allowed:
            log.warning("%s exit blocked for %s: %s", reason, sym, ", ".join(verdict.reasons))
            continue
        log.info("%s triggered for %s", reason, sym)
        for order in verdict.orders:
            _submit(c, state, order, positions, today, tag=reason)


def run_cycle(c: Components, state: AccountState, now: datetime) -> None:
    """Caller must set state.equity to the current value before each cycle. `now` is tz-aware.
    With a journal, every cycle that runs ends in a CycleSummary: "traded" or an explicit
    "no_trade" with the reasons (no candidates, rejected by risk, shadow only, ...)."""
    if c.journal is None:
        _run_cycle(c, state, now)
        return
    statuses: Counter = Counter()
    recorder, fills_before = c.recorder, c.fills

    def counting(idea: dict[str, Any]) -> None:
        statuses[idea.get("status") or "?"] += 1
        recorder(idea)

    c.recorder = counting
    try:
        ran = _run_cycle(c, state, now)
    finally:
        c.recorder = recorder
    if ran is None:
        return
    n_signals, n_candidates, snap = ran
    if snap:
        _journal(c, MarketSnapshot(type(c.market_data).__name__, snap))
    n_fills = c.fills - fills_before
    outcome = "traded" if n_fills else "no_trade"
    reasons = dict(statuses) if n_candidates else {"no_candidates": 1}
    _journal(c, CycleSummary(now.isoformat(), n_signals, n_candidates, n_fills, outcome, reasons))


def _run_cycle(c: Components, state: AccountState, now: datetime):
    today = market_hours.trading_date(now)
    c.risk.refresh(state, today)
    if c.settings.respect_market_hours and not market_hours.session_open(now):
        log.info("market closed at %s; skipping cycle", now.isoformat())
        return None
    allow_entries = not c.settings.respect_market_hours or (
        market_hours.minutes_to_close(now) > c.settings.risk.no_entries_last_minutes
    )
    halt = halted(c)
    if hasattr(c.market_data, "warm"):
        c.market_data.warm(c.universe)
    if c.context is not None and hasattr(c.context, "warm"):
        c.context.warm(c.universe)
    manage_positions(c, state, now)
    refresh_learning(c, state, now)
    if c.think:
        thoughts.observe(c, state, now)
    positions = {p.symbol: p for p in c.executor.positions()}
    apply_launcher(c, state, positions, now)
    held = frozenset(positions)

    kinds = required_event_kinds(c.strategies)
    events = fetch_all(c.providers, kinds, today - timedelta(days=EVENT_LOOKBACK_DAYS))
    ctx = StrategyContext(c.market_data, tuple(c.universe), tuple(events), today, held, now)

    signals: list[Signal] = []
    for ws in c.strategies:
        if c.risk.model_paused(state, ws.strategy.name):
            continue  # too many losses in a row today
        try:
            produced = _sleeve_signals(ws, ws.strategy.generate(ctx), state.sleeves)
        except Exception:
            log.exception("strategy %s failed", ws.strategy.name)
            continue
        signals.extend(produced)
        if produced and c.journal is not None:
            _journal(c, StrategyResult(ws.strategy.name, strategy_version(ws.strategy), ws.mode, [
                {"symbol": g.symbol, "direction": g.direction.value, "strength": g.strength,
                 "horizon_days": g.horizon_days, "evidence": g.evidence, "ts": g.ts.isoformat()}
                for g in produced]))
    weights = {ws.strategy.name: ws.weight for ws in c.strategies}
    standalone = frozenset(ws.strategy.name for ws in c.strategies if ws.standalone)
    shadow_models = frozenset(ws.strategy.name for ws in c.strategies if ws.mode == "shadow")
    def stale_or_blocked(cand: Candidate) -> bool:
        """Spend the review cap on ideas that can still trade: setups already reviewed today and
        names the earnings rule would refuse go to the back of the line."""
        buyers = tuple(sorted({s.strategy for s in cand.signals if s.direction is Direction.LONG}))
        return (today, cand.symbol, buyers) in c.reviewed or _earnings_block(c, cand, now) is not None

    candidates = combine(signals, weights, c.settings.ensemble, standalone, stale_or_blocked)
    log.info("%d events, %d signals, %d candidates", len(events), len(signals), len(candidates))

    snap: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        decision = None
        quote = c.market_data.quotes([cand.symbol]).get(cand.symbol)
        if quote is not None:
            snap[cand.symbol] = {"bid": quote.bid, "ask": quote.ask, "last": quote.last, "ts": quote.ts.isoformat()}
        if cand.direction is Direction.EXIT:
            if cand.symbol not in held:
                continue
            owner = (state.brackets.get(cand.symbol) or {}).get("model")
            if owner and owner not in cand.strategies:
                continue  # opened by another model: only its owner's exits (or its plan) apply
            verdict = c.risk.check_exit(cand, positions[cand.symbol], state, quote, today)
        else:
            if cand.symbol in held:  # trims always run; top-up buys respect HALT and the close cutoff
                _maybe_top_up(c, state, cand, positions[cand.symbol], quote, now, buys_ok=allow_entries and not halt)
                continue  # one position per symbol: no point paying for a review
            if halt:
                log.info("skipping entry %s: HALT file present", cand.symbol)
                c.recorder(_idea(now, cand, quote, None, None, "halted"))
                continue
            if not allow_entries:
                log.info("skipping entry %s: within %s min of close", cand.symbol,
                         c.settings.risk.no_entries_last_minutes)
                c.recorder(_idea(now, cand, quote, None, None, "too_close_to_close"))
                continue
            buyers = tuple(sorted({s.strategy for s in cand.signals if s.direction is Direction.LONG}))
            longs = [s for s in cand.signals if s.direction is Direction.LONG]
            systematic = bool(longs) and all(s.evidence.get("systematic") for s in longs)
            key = (today, cand.symbol, buyers)
            if key in c.reviewed and not systematic:
                continue  # same setup already reviewed today (rule-based entries are free: they retry)
            c.reviewed.add(key)
            report = c.context.next_earnings(cand.symbol) if c.context else None
            if _earnings_block(c, cand, now) is not None:
                # the hard rule would refuse it anyway: skip the paid review
                verdict = RiskVerdict(False, ("earnings_before_exit",))
                log.info("rejected long %s: earnings_before_exit (%s)", cand.symbol, report.isoformat())
                c.recorder(_idea(now, cand, quote, None, verdict, "rejected"))
                continue
            flags = c.context.red_flags(cand.symbol) if c.context and hasattr(c.context, "red_flags") else []
            if flags:
                verdict = RiskVerdict(False, ("sec_8k_red_flag",))
                log.info("rejected long %s: sec_8k_red_flag (%s)", cand.symbol, "; ".join(flags))
                c.recorder(_idea(now, cand, quote, None, verdict, "rejected"))
                continue
            account = {
                "equity": state.equity,
                "cash": state.cash,
                "open_positions": state.open_positions,
                "max_open_positions": c.settings.risk.max_open_positions,
                "held_symbols": sorted(held),
            }
            if c.risk.trail_giveback:
                account["exit_policy"] = (
                    f"autopilot: trailing stop, no fixed take-profit. Your stop_loss_pct sets the starting stop "
                    f"(at most {c.risk.max_loss_pct:.0%}) and the trail distance below the highest price "
                    f"(at most {c.risk.trail_giveback:.0%}); pick it from the stock's normal volatility")
            if systematic:
                # mechanical allocation rule: no Claude review; every hard risk check still runs
                decision = Decision(True, 1.0, "systematic rule (no review)", 0.0)
                adv = avg_dollar_volume(c.market_data, cand.symbol)
                verdict = c.risk.check_entry(cand, decision, state, quote, adv, held, today, report, now,
                                         live_quotes(c.market_data), **_entry_gates(c, cand))
                if not verdict.allowed:
                    log.info("rejected long %s: %s", cand.symbol, ", ".join(verdict.reasons))
                    c.recorder(_idea(now, cand, quote, decision, verdict, "rejected"))
                    continue
                if set(buyers) <= shadow_models:
                    log.info("shadow long %s (%s): systematic, passed risk, not executed", cand.symbol, ",".join(buyers))
                    c.recorder(_idea(now, cand, quote, decision, verdict, "shadow"))
                    continue
                if c.think:
                    thoughts.before_buy(c, state, now, cand, quote, decision, verdict)
                c.recorder(_idea(now, cand, quote, decision, verdict, "auto_submitted"))
                plan = c.risk.exit_plan(cand)
                for order in verdict.orders:
                    if _submit(c, state, order, positions, today, decision, plan, ",".join(cand.strategies)):
                        positions = {p.symbol: p for p in c.executor.positions()}
                        held = frozenset(positions)
                continue
            if set(buyers) <= shadow_models and not c.settings.review_shadow_ideas:
                # a shadow model never trades: record the idea without paying for a review
                log.info("shadow long %s (%s): not reviewed (REVIEW_SHADOW_IDEAS=false)", cand.symbol, ",".join(buyers))
                c.recorder(_idea(now, cand, quote, None, None, "shadow_unreviewed"))
                continue
            if llm_costs.spent(c.settings.llm_usage_path, today) >= c.settings.max_claude_usd_per_day:
                log.info("skipping review of %s: daily Claude spend cap $%.2f reached", cand.symbol,
                         c.settings.max_claude_usd_per_day)
                c.recorder(_idea(now, cand, quote, None, None, "review_budget_used"))
                continue
            used = c.reviews_today.get(today, 0)
            per_model = [c.reviews_today.get((today, m), 0) for m in buyers]
            if used >= c.settings.max_claude_reviews_per_day or (
                per_model and min(per_model) >= c.settings.max_reviews_per_model_per_day
            ):
                log.info("skipping review of %s (%s): daily Claude budget used", cand.symbol, ",".join(buyers))
                c.recorder(_idea(now, cand, quote, None, None, "review_budget_used"))
                continue
            c.reviews_today[today] = used + 1
            for m in buyers:
                c.reviews_today[(today, m)] = c.reviews_today.get((today, m), 0) + 1
            if c.context:
                account["market_context"] = c.context.for_symbol(cand.symbol)
                if hasattr(c.context, "plan_odds"):
                    odds = c.context.plan_odds(cand.symbol, c.risk.exit_plan(cand))
                    if odds:
                        account["replay_odds"] = odds
            if c.settings.review_outcome_memory:
                try:
                    account["outcome_memory"] = outcome_memory(
                        c.settings.paper_ledger_path, list(buyers), cand.symbol, now,
                        c.settings.ideas_path.with_name("idea_scores.json"))
                except Exception:  # noqa: BLE001 - memory is optional context, never a blocker
                    log.exception("outcome memory failed for %s", cand.symbol)
            decision = (c.engine.evaluate(cand, account, debate=True) if c.settings.review_debate
                        else c.engine.evaluate(cand, account))
            adv = avg_dollar_volume(c.market_data, cand.symbol)
            verdict = c.risk.check_entry(cand, decision, state, quote, adv, held, today, report, now,
                                         live_quotes(c.market_data), **_entry_gates(c, cand))

        if not verdict.allowed:
            log.info("rejected %s %s: %s", cand.direction.value, cand.symbol, ", ".join(verdict.reasons))
            c.recorder(_idea(now, cand, quote, decision, verdict, "rejected"))
            continue
        if cand.direction is Direction.LONG and shadow_models and {
            s.strategy for s in cand.signals if s.direction is Direction.LONG
        } <= shadow_models:
            log.info("shadow long %s (%s): reviewed and passed risk, not executed",
                     cand.symbol, ",".join(cand.strategies))
            c.recorder(_idea(now, cand, quote, decision, verdict, "shadow"))
            continue
        if cand.direction is Direction.LONG and c.settings.entry_mode == "manual":
            log.info("recommended long %s: awaiting your approval on the dashboard", cand.symbol)
            if c.think:
                thoughts.before_buy(c, state, now, cand, quote, decision, verdict, waiting=True)
            c.recorder(_idea(now, cand, quote, decision, verdict, "recommended"))
            continue
        if c.think and cand.direction is Direction.LONG:
            thoughts.before_buy(c, state, now, cand, quote, decision, verdict)
        c.recorder(_idea(now, cand, quote, decision, verdict, "auto_submitted"))
        plan = c.risk.exit_plan(cand) if cand.direction is Direction.LONG else None
        tag = ",".join(cand.strategies) if cand.direction is Direction.LONG else "signal_exit"
        for order in verdict.orders:
            if _submit(c, state, order, positions, today, decision, plan, tag):
                # Keep later candidates in this cycle checked against post-fill positions.
                positions = {p.symbol: p for p in c.executor.positions()}
                held = frozenset(positions)
    return len(signals), len(candidates), snap


def process_manual_order(
    c: Components, state: AccountState, order: dict[str, Any], now: datetime
) -> tuple[str, list[str], dict[str, Any] | None]:
    """Execute one dashboard click in the paper process. Returns (status, reasons, fill).

    A click replaces Claude's approval (you are the approver), but every hard risk rule
    still applies: kill switch, loss limits, caps, PDT guard, spread, liquidity, market hours.
    """
    from ctbot.manual_queue import ORDER_TTL_SECONDS

    today = market_hours.trading_date(now)
    c.risk.refresh(state, today)
    age = (now - datetime.fromisoformat(order["ts"])).total_seconds()
    if age > ORDER_TTL_SECONDS:
        return "expired", [f"click is {int(age)}s old; prices may have moved"], None
    if c.settings.respect_market_hours and not market_hours.session_open(now):
        return "rejected", ["market_closed"], None

    sym = order["symbol"].upper()
    positions = {p.symbol: p for p in c.executor.positions()}
    quote = c.market_data.quotes([sym]).get(sym)
    names = order.get("strategies") or ["manual"]

    if order["side"] == "sell":
        sig = Signal("manual", sym, Direction.EXIT, 1.0, 0, {"source": "dashboard"})
        verdict = c.risk.check_exit(Candidate(sym, Direction.EXIT, -1.0, (sig,)),
                                    positions.get(sym), state, quote, today)
        decision = None
    elif order["side"] == "buy":
        if halted(c):
            return "rejected", ["halt_file_present"], None
        if c.settings.respect_market_hours and (
            market_hours.minutes_to_close(now) <= c.settings.risk.no_entries_last_minutes
        ):
            return "rejected", ["too_close_to_close"], None
        sigs = tuple(Signal(n, sym, Direction.LONG, 1.0, 0, {"source": "dashboard"}) for n in names)
        notional = order.get("notional") or state.equity * c.settings.risk.max_position_pct
        decision = Decision(
            approve=True, confidence=1.0, rationale="approved by you on the dashboard",
            suggested_notional=float(notional), stop_loss_pct=order.get("stop_loss_pct"),
            take_profit_pct=order.get("take_profit_pct"),
        )
        adv = avg_dollar_volume(c.market_data, sym)
        report = c.context.next_earnings(sym) if c.context else None
        flags = c.context.red_flags(sym) if c.context and hasattr(c.context, "red_flags") else []
        verdict = c.risk.check_entry(Candidate(sym, Direction.LONG, 1.0, sigs), decision, state,
                                     quote, adv, frozenset(positions), today, report, now,
                                     live_quotes(c.market_data), red_flags=flags)
    else:
        return "rejected", [f"unknown side {order['side']!r}"], None

    if not verdict.allowed:
        return "rejected", list(verdict.reasons), None
    fill = None
    for o in verdict.orders:
        before = len(state.day_trade_dates)
        if _submit(c, state, o, positions, today, decision, tag="manual"):
            fill = {"symbol": o.symbol, "side": o.side.value, "notional": o.notional,
                    "day_trade": len(state.day_trade_dates) > before}
    if fill is None:
        return "rejected", ["no_fill (no quote or no cash)"], None
    return "filled", [], fill
