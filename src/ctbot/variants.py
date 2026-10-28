"""Side-by-side paper books: one bot, one set of market data, several $10k accounts with different
settings, so a week or two of forward trading shows which settings do best.

config/variants.toml lists the books. Each gets its own folder, data/variants/<name>/, holding the
same files the main book keeps in data/ (ledger, risk state, autopilot.json, ideas, journal, Claude
spend, equity history). The main paper bot runs every book right after its own cycle and checks
their stops on the same 30-second schedule:

* market data is fetched once (the books share the main bot's cached client), and alt-data
  providers are fetched once per cycle (CachedProvider);
* Claude reviews a setup once per day per model (SharedReviews): every book with the same
  CLAUDE_MODEL reuses that verdict, so books differ only in what they do with it;
* each book applies its own Autopilot settings and risk limits, and has its own kill switch and
  daily loss limit. data/HALT stops new entries in every book.

Paper only: books are PaperBrokers inside the paper process, and a book can't set live, mode,
path or key settings (ALLOWED_ENV).
"""

from __future__ import annotations

import json
import logging
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ctbot import autopilot

log = logging.getLogger(__name__)

CONFIG = Path("config/variants.toml")
ROOT_NAME = "variants"  # data/variants/<name>/, next to the main ledger
HALT_MARK = "mirrors data/HALT\n"
# Settings a book may change. Everything else (live flags, mode, keys, paths, market data) is the
# main bot's.
ALLOWED_ENV = frozenset({
    "CLAUDE_MODEL", "CLAUDE_EFFORT", "STARTING_CAPITAL", "MAX_TRADING_CAPITAL", "SIZE_BY",
    "MAX_POSITION_PCT", "MAX_OPEN_POSITIONS", "MAX_CANDIDATES_PER_CYCLE", "MIN_AGREEING_STRATEGIES",
    "MIN_LONG_SCORE", "MAX_CLAUDE_REVIEWS_PER_DAY", "MAX_REVIEWS_PER_MODEL_PER_DAY",
    "MAX_CLAUDE_USD_PER_DAY", "MAX_DAILY_LOSS_PCT", "MAX_TOTAL_DRAWDOWN_PCT", "MAX_SPREAD_PCT",
    "SWING_MAX_DAYS", "MODEL_MAX_LOSING_STREAK", "REVIEW_DEBATE", "REVIEW_OUTCOME_MEMORY",
    "PARTIAL_PROFIT_R", "VWAP_ENTRY_GATE", "REGIME_GATE",
})
NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(frozen=True)
class Variant:
    name: str
    title: str
    autopilot: autopilot.AutopilotConfig
    env: dict[str, str] = field(default_factory=dict)
    strategies: str = "library"  # "factory": trade only the strategy factory's live list


def load(path: Path = CONFIG) -> list[Variant]:
    """The books in config/variants.toml; [] when the file is missing or `enabled = false`.
    Raises ValueError for a bad name, an unknown setting or an Autopilot value out of range."""
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not raw.pop("enabled", True):
        return []
    out = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"variants: {name} must be a table")
        if not name or set(name) - NAME_OK:
            raise ValueError(f"variants: name {name!r} may use only a-z, 0-9 and _")
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        bad = set(env) - ALLOWED_ENV
        if bad:
            raise ValueError(f"variants: {name} can't set {', '.join(sorted(bad))}")
        ap = autopilot.validate({"enabled": True, **(spec.get("autopilot") or {})})
        strategies = str(spec.get("strategies", "library"))
        if strategies not in ("library", "factory"):
            raise ValueError(f"variants: {name}.strategies must be library or factory")
        out.append(Variant(name, str(spec.get("title") or name), ap, env, strategies))
    return out


def book_env(base: dict[str, str], main_ledger: Path, v: Variant) -> dict[str, str]:
    d = main_ledger.parent / ROOT_NAME / v.name
    return {**base, **v.env, "CTBOT_MODE": "paper", "LIVE_TRADING_ENABLED": "false", "ENTRY_MODE": "auto",
            "PAPER_LEDGER_PATH": str(d / "paper_ledger.json"),
            "PAPER_RISK_STATE_PATH": str(d / "paper_risk_state.json"), "LOG_DIR": str(d / "logs")}


class CachedProvider:
    """One fetch per cycle for every book: the result is reused for `ttl_s` seconds."""

    def __init__(self, inner: Any, ttl_s: float = 240.0) -> None:
        self.inner, self.ttl_s = inner, ttl_s
        self.name, self.supports = inner.name, inner.supports
        self._hits: dict[tuple, tuple[float, list]] = {}

    def fetch(self, kinds, since):
        key = (frozenset(kinds), since)
        hit = self._hits.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return list(hit[1])
        events = list(self.inner.fetch(kinds, since))
        self._hits[key] = (time.monotonic(), events)
        return events


def cached(providers: list[Any]) -> list[Any]:
    return [p if isinstance(p, CachedProvider) else CachedProvider(p) for p in providers]


class BookMarketData:
    """The books' view of the main bot's market data: a price or intraday-bar fetch is reused by
    every book for `ttl_s` seconds. The client's own quote cache lasts only seconds, so without
    this each book re-fetched the same quotes and a cycle took minutes. Daily bars are already
    cached for the day by the client."""

    CACHED = ("today_bars", "intraday_bars")

    def __init__(self, inner: Any, ttl_s: float = 60.0) -> None:
        self.inner, self.ttl_s = inner, ttl_s  # 60 s keeps quotes inside the 120 s stale-quote check
        self._quotes: dict[str, tuple[float, Any]] = {}
        self._calls: dict[tuple, tuple[float, Any]] = {}

    def quotes(self, symbols):
        mono = time.monotonic()
        syms = sorted({s.upper() for s in symbols})
        need = [s for s in syms if s not in self._quotes or mono - self._quotes[s][0] > self.ttl_s]
        if need:
            fresh = self.inner.quotes(need)
            for s in need:
                if s in fresh:
                    self._quotes[s] = (mono, fresh[s])
                else:
                    self._quotes.pop(s, None)
        return {s: self._quotes[s][1] for s in syms if s in self._quotes}

    def daily_bars(self, symbol, lookback_days):
        return self.inner.daily_bars(symbol, lookback_days)

    def is_market_open(self):
        return self.inner.is_market_open()

    def __getattr__(self, name):
        attr = getattr(self.inner, name)  # AttributeError when the client lacks it, as before
        if name not in self.CACHED:
            return attr

        def cached_call(*args, **kwargs):
            key = (name, repr(args), repr(sorted(kwargs.items())))
            hit = self._calls.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return hit[1]
            for k in [k for k, v in self._calls.items() if time.monotonic() - v[0] >= self.ttl_s]:
                del self._calls[k]
            out = attr(*args, **kwargs)
            self._calls[key] = (time.monotonic(), out)
            return out

        return cached_call


class SharedReviews:
    """Wraps a decision engine so a setup is reviewed once per day per model across all books."""

    def __init__(self, inner: Any, cache: dict) -> None:
        self.inner, self.cache = inner, cache

    def evaluate(self, candidate, account, debate: bool = False):
        day = datetime.now().date()
        key = (day, getattr(self.inner, "model", None), getattr(self.inner, "effort", None), candidate.symbol,
               tuple(sorted(candidate.strategies)), debate)
        for k in [k for k in self.cache if k[0] != day]:
            del self.cache[k]
        if key not in self.cache:
            self.cache[key] = (self.inner.evaluate(candidate, account, debate=True) if debate
                               else self.inner.evaluate(candidate, account))
        return self.cache[key]

    def reset_client(self) -> None:
        self.inner._client = None  # an API key changed on the dashboard


@dataclass
class Book:
    variant: Variant
    components: Any
    state: Any
    broker: Any
    report_mtime: float | None = None  # factory books: the report their models were built from

    @property
    def settings(self):
        return self.components.settings


def build(main: Any, reviews: dict) -> list[Book]:
    """Books for every variant in config/variants.toml, sharing the main bot's market data,
    providers, context and review cache. Writes each book's autopilot.json from the config."""
    import os

    from ctbot.config import Settings
    from ctbot.contracts import Journal
    from ctbot.decision.engine import ClaudeDecisionEngine
    from ctbot.library import factory
    from ctbot.manual_queue import _append
    from ctbot.paper.simulator import PaperBroker
    from ctbot.pipeline import Components
    from ctbot.risk.manager import AccountState, RiskManager, load_account_state
    from ctbot.strategies.registry import build_strategies, load_strategy_config, strategy_risk_overrides

    s0 = main.settings
    md = BookMarketData(main.market_data)  # one shared price snapshot for all books
    books = []
    for v in load(s0.strategies_file.with_name(CONFIG.name)):
        s = Settings.from_env(book_env(dict(os.environ), s0.paper_ledger_path, v))
        assert not s.live_trading_enabled and s.mode.value == "paper"
        s.paper_ledger_path.parent.mkdir(parents=True, exist_ok=True)
        autopilot.save(s.autopilot_path, v.autopilot)
        strategies = (factory.live_strategies(factory.load_report()) if v.strategies == "factory"
                      else build_strategies(load_strategy_config(s0.strategies_file)))
        scanners = {ws.strategy.name: getattr(ws.strategy, "scanner", None) for ws in main.strategies}
        for ws in strategies:
            if scanners.get(ws.strategy.name) is not None:
                ws.strategy.scanner = scanners[ws.strategy.name]  # one market-wide scan for all books
        broker = PaperBroker(md, s.starting_capital, s.paper_ledger_path,
                             instant_settlement=s.paper_settlement == "instant", margin=s.paper_margin)
        c = Components(
            settings=s, market_data=md, providers=main.providers, strategies=strategies,
            engine=SharedReviews(ClaudeDecisionEngine(s.claude_model, s.claude_effort, usage_path=s.llm_usage_path),
                                 reviews),
            risk=RiskManager(s.risk, strategy_risk_overrides(strategies)), executor=broker,
            universe=main.universe, recorder=lambda idea, p=s.ideas_path: _append(p, idea),
            context=main.context, journal=Journal(s.journal_path), think=False)  # Bub speaks for the main book
        equity = broker.equity()
        state = AccountState(equity=equity, cash=broker.cash(), peak_equity=max(equity, s.starting_capital),
                             day_start_equity=equity, open_positions=len(broker.positions()))
        load_account_state(state, s.paper_risk_state_path)
        books.append(Book(v, c, state, broker, _report_mtime() if v.strategies == "factory" else None))
        log.info("book %s: %s (max loss %.0f%%, give-back %.0f%%, Claude floor %.0f%%%s)", v.name, v.title,
                 v.autopilot.max_loss_pct * 100, v.autopilot.giveback_pct * 100, v.autopilot.min_confidence * 100,
                 "".join(f", {k}={val}" for k, val in v.env.items()))
    return books


def _sync_halt(main_halt: Path, book_halt: Path) -> None:
    """data/HALT stops entries in every book; a book's own HALT file is left alone."""
    try:
        mine = book_halt.read_text() == HALT_MARK
    except FileNotFoundError:
        mine = None
    if main_halt.exists() and mine is None:
        book_halt.write_text(HALT_MARK)
    elif not main_halt.exists() and mine:
        book_halt.unlink()


def _report_mtime() -> float | None:
    from ctbot.library import factory

    try:
        return factory.REPORT.stat().st_mtime
    except OSError:
        return None


def _reload_factory(b: Book) -> None:
    """A factory book picks up the nightly report's new live list; open positions keep their exits."""
    from ctbot.library import factory

    m = _report_mtime()
    if b.variant.strategies != "factory" or m == b.report_mtime:
        return
    b.report_mtime = m
    b.components.strategies = factory.live_strategies(factory.load_report())
    b.components.configured = b.components.launched = None  # the autopilot re-launches from the new list
    log.info("book %s: factory models now %s", b.variant.name,
             ", ".join(ws.strategy.name for ws in b.components.strategies) or "none")


def _refresh(b: Book) -> None:
    from ctbot.pipeline import sync_cash

    b.state.equity = b.broker.equity()
    sync_cash(b.broker, b.state)
    b.state.open_positions = len(b.broker.positions())


def run(books: list[Book], main: Any, now: datetime, record_equity) -> None:
    """One cycle for every book, after the main bot's. One book failing never stops the others."""
    from ctbot.pipeline import run_cycle
    from ctbot.risk.manager import save_account_state

    for b in books:
        try:
            b.components.providers, b.components.context = main.providers, main.context
            _sync_halt(main.settings.halt_path, b.settings.halt_path)
            _reload_factory(b)
            _refresh(b)
            run_cycle(b.components, b.state, now)
            save_account_state(b.state, b.settings.paper_risk_state_path)
            record_equity(b.settings, b.broker)
        except Exception:  # noqa: BLE001
            log.exception("book %s cycle failed; continuing", b.variant.name)


def manage(books: list[Book], now: datetime) -> None:
    """The 30-second stop check, for books holding positions."""
    from ctbot.pipeline import manage_positions
    from ctbot.risk.manager import save_account_state

    for b in books:
        if not b.broker.positions():
            continue
        try:
            _refresh(b)
            manage_positions(b.components, b.state, now)
            save_account_state(b.state, b.settings.paper_risk_state_path)
        except Exception:  # noqa: BLE001
            log.exception("book %s position check failed; continuing", b.variant.name)


# ---- results ---------------------------------------------------------------------------------

_CACHE: dict[tuple, tuple[tuple, Any]] = {}  # (fn, args) -> (file versions, result)


def _cached(fn: Any, files: list[Path], *args: Any) -> Any:
    """fn(*args), re-run only once one of `files` changes: the dashboard asks every 10 s, and
    re-parsing every book's full history each time took most of a second on four weeks of data."""
    def version(f: Path) -> tuple[int, int] | None:
        try:
            st = f.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    stamp, key = tuple(version(f) for f in files), (fn.__name__, *args)
    hit = _CACHE.get(key)
    if hit is None or hit[0] != stamp:
        hit = _CACHE[key] = (stamp, fn(*args))
    return hit[1]


def _book_files(ledger: Path) -> list[Path]:
    return [ledger, ledger.with_name("equity_history.jsonl"), ledger.with_name("llm_usage.jsonl")]

def _book_summary(ledger: Path, start: float) -> dict[str, Any]:
    from ctbot.dashboard.state import model_stats

    hist = []
    try:
        hist = [json.loads(ln) for ln in ledger.with_name("equity_history.jsonl").read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        pass
    eq = [h["equity"] - h.get("deposits", 0.0) for h in hist] or [start]  # top-ups are not gains
    peak, dd = start, 0.0
    for e in eq:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    try:
        fills = json.loads(ledger.read_text()).get("fills", [])
    except (OSError, ValueError):
        fills = []
    stats = model_stats(fills, {})
    closed = sum(m["closed"] for m in stats)
    wins = sum(m["wins"] for m in stats)
    spent = 0.0  # Claude spend charged to this book, all days (shared reviews are charged once)
    try:
        for ln in ledger.with_name("llm_usage.jsonl").read_text().splitlines():
            try:
                spent += float(json.loads(ln).get("usd", 0))
            except ValueError:
                continue
    except OSError:
        pass
    return {"equity": round(eq[-1], 2), "return": eq[-1] / start - 1 if start else 0.0, "max_dd": dd,
            "trades": closed, "win_rate": wins / closed if closed else None,
            "open": hist[-1].get("positions", 0) if hist else 0, "since": hist[0]["ts"] if hist else None,
            "claude_usd": round(spent, 4)}


def summary(main_settings: Any, path: Path | None = None) -> list[dict[str, Any]]:
    """The main book plus every variant, best return first."""
    from ctbot.config import Settings

    main_ap = autopilot.load(main_settings.autopilot_path)
    rows = [{"name": "main", "title": "Your main account", "max_loss_pct": main_ap.max_loss_pct,
             "giveback_pct": main_ap.giveback_pct, "min_confidence": main_ap.min_confidence,
             "env": {"CLAUDE_MODEL": main_settings.claude_model},
             **_cached(_book_summary, _book_files(main_settings.paper_ledger_path),
                         main_settings.paper_ledger_path, main_settings.starting_capital)}]
    try:
        variants = load(path or main_settings.strategies_file.with_name(CONFIG.name))
    except (ValueError, OSError) as e:
        log.warning("variants: %s", e)
        variants = []
    for v in variants:
        s = Settings.from_env(book_env({}, main_settings.paper_ledger_path, v))
        start = float(v.env.get("STARTING_CAPITAL", main_settings.starting_capital))
        rows.append({"name": v.name, "title": v.title, "max_loss_pct": v.autopilot.max_loss_pct,
                     "giveback_pct": v.autopilot.giveback_pct, "min_confidence": v.autopilot.min_confidence,
                     "env": v.env, **_cached(_book_summary, _book_files(s.paper_ledger_path), s.paper_ledger_path, start)})
    _vs_main(rows, main_settings.paper_ledger_path.parent)
    return sorted(rows, key=lambda r: -r["return"])


MIN_DAYS_TO_JUDGE = 10  # fewer daily returns than this and the t-stat's normal approximation is meaningless


def _daily_returns(history: Path) -> dict[str, float]:
    """date -> that day's return, from the last equity point of each day."""
    last: dict[str, tuple[float, float]] = {}
    try:
        for ln in history.read_text().splitlines():
            try:
                h = json.loads(ln)
                last[h["ts"][:10]] = (float(h["equity"]), float(h.get("deposits", 0.0)))
            except (ValueError, KeyError):
                continue
    except OSError:
        return {}
    days = sorted(last)
    # a top-up (paper deposit) that day is not a return
    return {d: (last[d][0] - (last[d][1] - last[p][1])) / last[p][0] - 1 for p, d in zip(days, days[1:]) if last[p][0]}


def _vs_main(rows: list[dict[str, Any]], data_dir: Path) -> None:
    """Adds row["vs_main"] = {"days", "edge" (mean daily return over main), "verdict"} to each book.
    Most published strategies don't beat zero on their own data (awesome-systematic-trading's
    replication record), so a book only counts as better or worse than main when its daily edge
    survives a t-test with Benjamini-Hochberg correction across all books; otherwise "too early"."""
    from ctbot.research.validation import benjamini_hochberg, sharpe_pvalue

    import numpy as np

    main_h = data_dir / "equity_history.jsonl"
    main = _cached(_daily_returns, [main_h], main_h)
    diffs = {}
    for r in rows[1:]:
        h = data_dir / ROOT_NAME / r["name"] / "equity_history.jsonl"
        mine = _cached(_daily_returns, [h], h)
        diffs[r["name"]] = np.array([mine[d] - main[d] for d in sorted(mine.keys() & main.keys())])
    pvals = {n: 2 * min(sharpe_pvalue(d), sharpe_pvalue(-d)) for n, d in diffs.items() if len(d) >= MIN_DAYS_TO_JUDGE}
    keep = benjamini_hochberg(pvals) if pvals else {}
    for r in rows[1:]:
        d = diffs[r["name"]]
        edge = float(d.mean()) if len(d) else None
        verdict = ("better" if edge > 0 else "worse") if keep.get(r["name"]) else "too early"
        r["vs_main"] = {"days": len(d), "edge": edge, "verdict": verdict}


def main() -> None:
    """`ctbot-variants`: the side-by-side results table."""
    from ctbot.config import Settings

    rows = summary(Settings.from_env())
    print(f"{'book':<16}{'return':>8}{'worst':>8}{'trades':>8}{'won':>6}{'vs main':>16}  {'settings'}")
    for r in rows:
        won = "-" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
        extra = " ".join(f"{k}={v}" for k, v in r["env"].items())
        vs = r.get("vs_main")
        vs = f"{vs['verdict']} ({vs['days']}d)" if vs else "-"
        print(f"{r['name']:<16}{r['return']:>+8.1%}{r['max_dd']:>8.1%}{r['trades']:>8}{won:>6}{vs:>16}  "
              f"loss {r['max_loss_pct']:.0%} give {r['giveback_pct']:.0%} floor {r['min_confidence']:.0%} {extra}")

