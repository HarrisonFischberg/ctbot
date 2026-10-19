"""Paper-trading entrypoint (`ctbot-paper`). Never submits real orders.

The isolation guard is installed BEFORE any other ctbot import so nothing in this
process can load ctbot.live.
"""

from ctbot.execution.isolation import install_paper_guard

install_paper_guard()

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from ctbot.config import Settings, load_universe  # noqa: E402
from ctbot.decision.engine import ClaudeDecisionEngine  # noqa: E402
from ctbot.market_data.csv_provider import CsvMarketData  # noqa: E402
from ctbot.paper.simulator import PaperBroker  # noqa: E402
from ctbot import manual_queue, market_hours  # noqa: E402
from ctbot.pipeline import Components, manage_positions, process_manual_order, run_cycle  # noqa: E402
from ctbot.providers.registry import build_providers  # noqa: E402
from ctbot.risk.manager import (  # noqa: E402
    AccountState,
    RiskManager,
    load_account_state,
    save_account_state,
)
from ctbot.strategies.registry import (  # noqa: E402
    build_strategies,
    load_strategy_config,
    strategy_risk_overrides,
)


def _news():
    """Marketaux sentiment when a token is set (in .env or on the dashboard's Settings tab)."""
    if not os.environ.get("MARKETAUX_API_TOKEN"):
        return None
    from ctbot.market_data.news import MarketauxSentiment

    return MarketauxSentiment(os.environ["MARKETAUX_API_TOKEN"])


def _market_context(settings: Settings, call_many, md):
    """Robinhood context plus the optional public sources (TradingView, Marketaux)."""
    from ctbot.market_data.context import MarketContext

    snapshot = None
    if settings.tradingview_data:
        from ctbot.market_data.tradingview import TradingViewSnapshot

        snapshot = TradingViewSnapshot()
    return MarketContext(call_many, md, snapshot=snapshot, news=_news())


def main() -> None:
    parser = argparse.ArgumentParser(description="ctbot paper trading")
    parser.add_argument("--loop", action="store_true", help="repeat every --interval seconds")
    parser.add_argument("--interval", type=int, default=900)
    args = parser.parse_args()

    settings = Settings.from_env()
    assert not settings.live_trading_enabled, "paper process must have live disabled"
    lock = _single_instance(settings)  # noqa: F841 - held for the life of the process
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(settings.paper_log_path)],
    )
    for noisy in ("httpx", "httpx2"):  # one line per API call is noise
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if settings.market_data == "robinhood":
        from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient
        from ctbot.market_data.robinhood_readonly import RobinhoodReadOnlyMarketData

        from ctbot.market_data.failover import (
            DailyBarFailover, FirstBackup, TradingFlowDailyBars, WebullDailyBars)
        from ctbot.market_data.tradingflow_client import TradingFlowClient
        from ctbot.market_data.webull_client import WebullClient

        rh = ReadOnlyRobinhoodClient(interactive=False)
        # Webull, then TradingFlow, back up completed daily bars once their keys are set (read per call).
        md = DailyBarFailover(RobinhoodReadOnlyMarketData(rh.call_many),
                              FirstBackup(WebullDailyBars(WebullClient()),
                                          TradingFlowDailyBars(TradingFlowClient().call_many)))
        context = _market_context(settings, rh.call_many, md)
    elif settings.market_data == "csv":
        md, context = CsvMarketData(settings.bars_dir), None
    else:
        raise SystemExit(f"unknown MARKET_DATA {settings.market_data!r} (use csv or robinhood)")
    broker = PaperBroker(md, settings.starting_capital, settings.paper_ledger_path,
                         instant_settlement=settings.paper_settlement == "instant")
    strategies = build_strategies(load_strategy_config(settings.strategies_file))
    if settings.market_data == "robinhood":
        from ctbot.market_data.scanner import StocksInPlayScanner

        for ws in strategies:  # the market-wide scan needs the Robinhood client
            if ws.strategy.name == "orb_sip":
                ws.strategy.scanner = StocksInPlayScanner(rh.call_many, top_k=ws.strategy.top_k)
    components = Components(
        settings=settings,
        market_data=md,
        providers=build_providers(list(settings.providers), dict(os.environ)),
        strategies=strategies,
        engine=ClaudeDecisionEngine(settings.claude_model, settings.claude_effort),
        risk=RiskManager(settings.risk, strategy_risk_overrides(strategies)),
        executor=broker,
        universe=load_universe(settings.universe_file),
        recorder=lambda idea: manual_queue._append(settings.ideas_path, idea),
        context=context,
    )
    equity = broker.equity()
    state = AccountState(
        equity=equity,
        cash=broker.cash(),
        peak_equity=max(equity, settings.starting_capital),
        day_start_equity=equity,
        open_positions=len(broker.positions()),
    )
    load_account_state(state, settings.paper_risk_state_path)

    log = logging.getLogger("ctbot.paper")
    log.info("paper loop started: entry_mode=%s market_data=%s", settings.entry_mode,
             settings.market_data)

    def refresh_state() -> None:
        state.equity, state.cash = broker.equity(), broker.cash()
        state.open_positions = len(broker.positions())

    def reload_keys() -> None:
        """Keys saved on the dashboard's Settings tab apply within seconds, no restart."""
        from ctbot import secret_store

        if secret_store.reload_if_changed():
            components.engine._client = None  # next review builds a client with the new key
            components.providers = build_providers(list(settings.providers), dict(os.environ))
            if components.context is not None:
                components.context.news = _news()
            log.info("settings changed on the dashboard: API keys reloaded")

    asleep_until: datetime | None = None

    def market_open() -> bool:
        return not settings.respect_market_hours or market_hours.session_open(datetime.now(timezone.utc))

    def handle_clicks() -> None:
        reload_keys()
        _heartbeat(settings, args.interval, asleep_until)
        for order in manual_queue.pending(settings.manual_orders_path, settings.manual_results_path):
            if market_open():  # a closed market rejects the click before prices matter
                refresh_state()
            try:
                status, reasons, fill = process_manual_order(
                    components, state, order, datetime.now(timezone.utc))
            except Exception as e:  # noqa: BLE001 - one bad click must not stop the loop
                log.exception("manual order %s failed", order.get("id"))
                status, reasons, fill = "error", [f"{type(e).__name__}: {e}"], None
            manual_queue.record_result(settings.manual_results_path, order, status, reasons, fill)
            log.info("dashboard %s %s: %s %s", order.get("side"), order.get("symbol"), status,
                     ", ".join(reasons))
            save_account_state(state, settings.paper_risk_state_path)
            if fill:
                _record_equity(settings, broker)

    def guarded(fn, what: str) -> None:
        """One failed cycle (data outage, expired sign-in, API error) must not end the loop."""
        try:
            fn()
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s failed; continuing", what)

    def cycle() -> None:
        refresh_state()
        # run_cycle -> RiskManager.refresh updates peak, rolls the day, trips the kill switch.
        run_cycle(components, state, datetime.now(timezone.utc))
        save_account_state(state, settings.paper_risk_state_path)
        _record_equity(settings, broker)

    def wait_for_open() -> None:
        """Market closed: no cycles, no quotes, and the Mac may sleep. Dashboard clicks are still
        answered (rejected as market_closed). Checks the wall clock, so it resumes on time after
        the Mac sleeps."""
        nonlocal asleep_until
        asleep_until = market_hours.next_open(datetime.now(timezone.utc))
        log.info("market closed: sleeping until %s (no quotes; the Mac may sleep)",
                 asleep_until.strftime("%a %b %d %H:%M ET"))
        while datetime.now(timezone.utc) < asleep_until:
            guarded(handle_clicks, "dashboard clicks")
            time.sleep(2)
        asleep_until = None
        log.info("market open: resuming")

    while True:
        guarded(handle_clicks, "dashboard clicks")
        guarded(cycle, "cycle")  # after the close this one only records the closing equity
        if not args.loop:
            guarded(handle_clicks, "dashboard clicks")
            break
        if not market_open():
            wait_for_open()
            continue
        deadline = time.monotonic() + args.interval
        next_check = time.monotonic() + 30
        while time.monotonic() < deadline and market_open():  # dashboard clicks run within ~2s
            guarded(handle_clicks, "dashboard clicks")
            if time.monotonic() >= next_check and broker.positions():
                guarded(lambda: (refresh_state(), manage_positions(components, state, datetime.now(timezone.utc)),
                                 save_account_state(state, settings.paper_risk_state_path)), "position check")
                next_check = time.monotonic() + 30
            time.sleep(2)


def _single_instance(settings: Settings):
    """Two paper bots on one ledger overwrite each other's fills; refuse to start a second."""
    import fcntl

    path = settings.paper_ledger_path.with_name("paper_bot.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another ctbot-paper is already running on this ledger; stop it first") from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _heartbeat(settings: Settings, interval: int, asleep_until: datetime | None = None) -> None:
    data = {"ts": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "interval": interval,
            "asleep_until": asleep_until.isoformat() if asleep_until else None}
    settings.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.heartbeat_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(settings.heartbeat_path)


def _record_equity(settings: Settings, broker: PaperBroker) -> None:
    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": round(broker.equity(), 4),
        "cash": round(broker.cash(), 4),
        "unsettled": round(broker.unsettled_cash(), 4),
        "positions": len(broker.positions()),
    }
    settings.equity_history_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.equity_history_path.open("a") as fh:
        fh.write(json.dumps(snap) + "\n")


if __name__ == "__main__":
    main()
