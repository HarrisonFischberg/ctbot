"""Deterministic risk checks. Runs after Claude and before any executor. Fails closed."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ctbot.config import RiskLimits
from ctbot.market_hours import trading_date
from ctbot.models import Candidate, Decision, Direction, OrderRequest, Position, Quote, Side


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reasons: tuple[str, ...]
    orders: tuple[OrderRequest, ...] = ()


@dataclass
class AccountState:
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    open_positions: int
    day_trade_dates: list[date] = field(default_factory=list)
    kill_switch: bool = False
    day_start_date: date | None = None
    # symbol -> {"stop": price, "target": price or None}; enforced every cycle.
    brackets: dict[str, dict[str, float | None]] = field(default_factory=dict)
    # model -> consecutive losing exits today; a model at the limit is paused until tomorrow
    model_losing_streak: dict[str, int] = field(default_factory=dict)
    # Paper launcher (ctbot.allocations): model -> its capital, and $ it holds now (at cost).
    # Set by the pipeline every cycle; not persisted.
    sleeves: dict[str, float] = field(default_factory=dict)
    sleeve_used: dict[str, float] = field(default_factory=dict)
    sleeve_reserve: dict[str, float] = field(default_factory=dict)  # model -> its unspent capital


def save_account_state(state: AccountState, path: Path) -> None:
    """Persist what must survive a restart; equity, cash and positions come from the broker."""
    data = {
        "peak_equity": state.peak_equity,
        "day_start_equity": state.day_start_equity,
        "day_start_date": state.day_start_date.isoformat() if state.day_start_date else None,
        "day_trade_dates": [d.isoformat() for d in state.day_trade_dates],
        "kill_switch": state.kill_switch,
        "brackets": state.brackets,
        "model_losing_streak": state.model_losing_streak,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_account_state(state: AccountState, path: Path) -> AccountState:
    """Overlay persisted durable fields onto `state` (built from current broker values)."""
    if not path.exists():
        return state
    data = json.loads(path.read_text())
    state.peak_equity = max(state.peak_equity, float(data["peak_equity"]))
    state.day_start_equity = float(data["day_start_equity"])
    raw_day = data.get("day_start_date")
    state.day_start_date = date.fromisoformat(raw_day) if raw_day else None
    state.day_trade_dates = [date.fromisoformat(d) for d in data.get("day_trade_dates", [])]
    # Latched: once tripped, only a human deleting the state file clears it.
    state.kill_switch = state.kill_switch or bool(data.get("kill_switch", False))
    state.brackets = data.get("brackets", {})
    state.model_losing_streak = data.get("model_losing_streak", {})
    return state


# The only limits a strategy may relax (via a [<name>.risk] table in strategies.toml).
LIQUIDITY_OVERRIDE_KEYS = frozenset({"min_share_price", "min_avg_dollar_volume"})


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        strategy_overrides: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.limits = limits
        self.strategy_overrides = strategy_overrides or {}

    def liquidity_limits(self, candidate: Candidate) -> tuple[float, float]:
        """(min_share_price, min_avg_dollar_volume) for this candidate.

        Each limit is the lowest of the global value and the overrides of strategies with a
        LONG signal in the candidate, so overrides can only loosen, never tighten.
        """
        price = self.limits.min_share_price
        adv = self.limits.min_avg_dollar_volume
        for s in candidate.signals:
            o = self.strategy_overrides.get(s.strategy)
            if not o or s.direction is not Direction.LONG:
                continue
            price = min(price, o.get("min_share_price", price))
            adv = min(adv, o.get("min_avg_dollar_volume", adv))
        return price, adv

    def refresh(self, state: AccountState, today: date) -> None:
        """Call at the start of every cycle, after state.equity is current."""
        state.peak_equity = max(state.peak_equity, state.equity)
        if state.day_start_date != today:
            state.day_start_date = today
            state.day_start_equity = state.equity
            state.model_losing_streak = {}  # paused models get a fresh start each day
        if state.equity <= state.peak_equity * (1 - self.limits.max_total_drawdown_pct):
            state.kill_switch = True
        cutoff = today - timedelta(days=7)
        state.day_trade_dates = [d for d in state.day_trade_dates if d > cutoff]

    def record_exit_fill(self, state: AccountState, position: Position, today: date,
                         exit_price: float | None = None) -> None:
        """A sell of a position opened today is a day trade (counts toward the PDT limit).
        Also updates the losing streak of the model that opened the position."""
        if trading_date(position.opened_at) == today:
            state.day_trade_dates.append(today)
        model = (state.brackets.get(position.symbol) or {}).get("model")
        if model and exit_price is not None and position.avg_cost:
            lost = exit_price < position.avg_cost
            state.model_losing_streak[model] = state.model_losing_streak.get(model, 0) + 1 if lost else 0
        state.brackets.pop(position.symbol, None)

    def model_paused(self, state: AccountState, model: str) -> bool:
        n = self.limits.model_max_losing_streak
        return n > 0 and state.model_losing_streak.get(model, 0) >= n

    def set_bracket(
        self, state: AccountState, symbol: str, fill_price: float, decision: Decision | None,
        plan: dict | None = None,
    ) -> None:
        """A model's exit plan (stop/target/exit_at) wins; otherwise Claude's stop (capped) or
        the default stop, plus Claude's target if it gave one. Stops are never wider than
        max_stop_loss_pct from the fill."""
        L = self.limits
        floor = fill_price * (1 - L.max_stop_loss_pct)
        if plan and plan.get("stop") is not None:
            stop = max(min(float(plan["stop"]), fill_price * 0.999), floor)
            tgt = plan.get("target")
            target = float(tgt) if tgt is not None and float(tgt) > fill_price else None
            state.brackets[symbol] = {"stop": round(stop, 4), "target": round(target, 4) if target else None,
                                      "exit_at": plan.get("exit_at"), "model": plan.get("model")}
            return
        stop_pct = decision.stop_loss_pct if decision and decision.stop_loss_pct else None
        stop_pct = min(stop_pct or L.default_stop_loss_pct, L.max_stop_loss_pct)
        tp_pct = decision.take_profit_pct if decision else None
        state.brackets[symbol] = {
            "stop": round(fill_price * (1 - stop_pct), 4),
            "target": round(fill_price * (1 + tp_pct), 4) if tp_pct else None,
        }

    def bracket_hit(self, state: AccountState, position: Position, quote: Quote,
                    now: datetime | None = None) -> str | None:
        """'stop_loss' / 'take_profit' if the bid crossed the bracket; 'time_exit' once a
        model's exit_at has passed. Positions with no bracket get a default stop off avg cost."""
        if position.symbol not in state.brackets:
            self.set_bracket(state, position.symbol, position.avg_cost, None)
        b = state.brackets[position.symbol]
        if quote.bid <= b["stop"]:
            return "stop_loss"
        if b.get("target") is not None and quote.bid >= b["target"]:
            return "take_profit"
        if now is not None and b.get("exit_at") and now >= datetime.fromisoformat(b["exit_at"]):
            return "time_exit"
        return None

    def maybe_swing(self, state: AccountState, position: Position, quote: Quote,
                    now: datetime, next_earnings: datetime | None = None) -> bool:
        """Let a winner run: within 15 minutes before a model's exit time, if the position is up
        at least swing_min_profit_pct, push the exit to the next session's 15:50 ET close and
        raise the stop to lock in a small gain (entry +0.2%). Limited to swing_max_days."""
        from ctbot.market_hours import ET, next_business_day

        L, b = self.limits, state.brackets.get(position.symbol)
        if not b or not b.get("exit_at") or L.swing_max_days <= 0 or state.kill_switch:
            return False
        exit_at = datetime.fromisoformat(b["exit_at"])
        if not (exit_at - timedelta(minutes=15) <= now < exit_at):
            return False
        if b.get("swings", 0) >= L.swing_max_days or not position.avg_cost:
            return False
        if quote.bid / position.avg_cost - 1 < L.swing_min_profit_pct:
            return False
        et = exit_at.astimezone(ET)
        # a morning exit (overnight model) extends to that day's close; a closing exit to tomorrow's
        new_day = et.date() if et.hour < 12 else next_business_day(et.date())
        new_exit = datetime.combine(new_day, datetime.min.time().replace(hour=15, minute=50), tzinfo=ET)
        if next_earnings is not None and exit_at < next_earnings < new_exit:
            return False  # would hold through an earnings report
        b["exit_at"] = new_exit.isoformat()
        b["stop"] = round(max(b["stop"], position.avg_cost * 1.002), 4)
        b["swings"] = b.get("swings", 0) + 1
        return True

    @staticmethod
    def exit_plan(candidate: Candidate) -> dict | None:
        """First signal in the candidate that carries an exit plan (intraday models)."""
        for s in candidate.signals:
            if s.direction is Direction.LONG and "exit_at" in s.evidence:
                return {**s.evidence, "model": s.strategy}
        return None

    def day_trades_last_5d(self, state: AccountState, today: date | None = None) -> int:
        today = today or datetime.now(timezone.utc).date()
        cutoff = today - timedelta(days=7)  # ~5 business days
        return sum(1 for d in state.day_trade_dates if d > cutoff)

    def check_entry(
        self,
        candidate: Candidate,
        decision: Decision,
        state: AccountState,
        quote: Quote | None,
        avg_dollar_volume: float,
        held_symbols: frozenset[str] = frozenset(),
        today: date | None = None,
        next_earnings: datetime | None = None,
        now: datetime | None = None,
    ) -> RiskVerdict:
        L = self.limits
        reasons: list[str] = []

        if candidate.direction is not Direction.LONG:
            reasons.append(f"not_an_entry_{candidate.direction.value}")
        if candidate.requires_short and not L.allow_short:
            reasons.append("short_not_allowed_in_cash_account")
        if state.kill_switch:
            reasons.append("kill_switch_active")
        if not decision.approve:
            reasons.append("claude_rejected")
        if decision.confidence < L.min_claude_confidence:
            reasons.append(f"confidence_{decision.confidence:.2f}_below_{L.min_claude_confidence}")
        if candidate.symbol in held_symbols:
            reasons.append("already_held")
        plan = self.exit_plan(candidate)
        if plan:  # a planned same-day exit needs a day trade still available
            exit_day = trading_date(datetime.fromisoformat(plan["exit_at"]))
            today = today or trading_date(datetime.now(timezone.utc))
            if exit_day == today and self.day_trades_last_5d(state, today) >= L.max_day_trades_5d:
                reasons.append("pdt_budget_used")
        if next_earnings is not None:  # never hold through an earnings report
            now = now or datetime.now(timezone.utc)
            horizon = max((s.horizon_days for s in candidate.signals if s.direction is Direction.LONG), default=1.0)
            planned_exit = datetime.fromisoformat(plan["exit_at"]) if plan else now + timedelta(days=horizon)
            if now < next_earnings < planned_exit:
                reasons.append("earnings_before_exit")
        if state.open_positions >= L.max_open_positions:
            reasons.append("max_open_positions")
        if state.equity <= state.day_start_equity * (1 - L.max_daily_loss_pct):
            reasons.append("daily_loss_limit")
        if state.equity <= state.peak_equity * (1 - L.max_total_drawdown_pct):
            reasons.append("max_drawdown_kill")
        min_price, min_adv = self.liquidity_limits(candidate)
        if quote is None:
            reasons.append("no_quote")
        else:
            if now is not None and (now - quote.ts).total_seconds() > L.max_quote_age_s:
                reasons.append("stale_quote")  # the pipeline and dashboard path always pass now
            if quote.last < min_price:
                reasons.append("price_below_min")
            if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
                reasons.append("bad_quote")
            elif (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) > L.max_spread_pct:
                reasons.append("spread_too_wide")
        if avg_dollar_volume < min_adv:
            reasons.append("illiquid")

        # Size off at most max_trading_capital, and never deploy more than it in total.
        base = min(state.equity, L.max_trading_capital)
        invested = max(0.0, state.equity - state.cash)  # includes unsettled cash: conservative
        room = L.max_trading_capital - invested
        # A model may request its own size (e.g. the trend core), capped at max_core_pct.
        req = [s.evidence.get("size_pct") for s in candidate.signals
               if s.direction is Direction.LONG and s.evidence.get("size_pct")]
        cap, cash = L.max_core_pct, state.cash
        owners = [s.strategy for s in candidate.signals
                  if s.direction is Direction.LONG and s.strategy in state.sleeves]
        if owners:  # launcher: size off the strategy's own capital and never hold more than it
            base, cap = state.sleeves[owners[0]], 1.0
            room = min(room, base - state.sleeve_used.get(owners[0], 0.0))
            # other strategies' unspent money is kept for them
            cash -= sum(v for k, v in state.sleeve_reserve.items() if k != owners[0])
        size = base * (min(max(req), cap) if req else L.max_position_pct)
        if L.size_by == "claude" and not req:  # Claude's suggestion can only shrink the size
            size = min(size, decision.suggested_notional)
        notional = min(size, cash, room)
        if room < 1.0:
            reasons.append("trading_capital_cap")
        elif notional < 1.0:
            reasons.append("notional_too_small")

        if reasons:
            return RiskVerdict(False, tuple(reasons))
        order = OrderRequest(
            symbol=candidate.symbol,
            side=Side.BUY,
            notional=round(notional, 2),
            limit_price=round(quote.ask * (1 + L.entry_limit_slack_pct), 4),
        )
        return RiskVerdict(True, (), (order,))

    def check_exit(
        self,
        candidate: Candidate,
        position: Position | None,
        state: AccountState,
        quote: Quote | None,
        today: date | None = None,
    ) -> RiskVerdict:
        """Exits reduce risk, so they bypass Claude and loss limits, but not the PDT guard."""
        today = today or datetime.now(timezone.utc).date()
        reasons: list[str] = []
        if position is None or position.quantity <= 0:
            reasons.append("no_position")
        if quote is None:
            reasons.append("no_quote")
        if (
            position is not None
            and trading_date(position.opened_at) == today
            and self.day_trades_last_5d(state, today) >= self.limits.max_day_trades_5d
        ):
            reasons.append("pdt_guard")
        if reasons:
            return RiskVerdict(False, tuple(reasons))
        order = OrderRequest(
            symbol=candidate.symbol,
            side=Side.SELL,
            notional=round(position.quantity * quote.bid, 2),
            limit_price=quote.bid,
        )
        return RiskVerdict(True, (), (order,))
