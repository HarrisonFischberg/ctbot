"""Settings loaded from environment (.env). Safe defaults: paper mode, live disabled."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ctbot.aggregation.ensemble import EnsembleParams

LIVE_CONFIRM_PHRASE = "I understand real money is at risk"



class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    # Only the exact lowercase string "true" enables a flag; anything else is false.
    return raw.strip().lower() == "true"


def _float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    return float(raw) if raw not in (None, "") else default


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.20
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    max_total_drawdown_pct: float = 0.15
    min_share_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000
    allow_short: bool = False
    max_day_trades_5d: int = 3
    min_claude_confidence: float = 0.65
    max_spread_pct: float = 0.01          # (ask - bid) / mid; entries only
    # Buy limit = ask at decision time x (1 + this). Room for the price to move during the Claude
    # review (~10 s); without it, breakouts that keep rising were skipped as unfilled.
    entry_limit_slack_pct: float = 0.002
    default_stop_loss_pct: float = 0.05   # used when Claude gives no stop
    max_stop_loss_pct: float = 0.10       # caps a wider stop from Claude
    no_entries_last_minutes: float = 15.0 # no new entries this close to the 16:00 ET close
    max_trading_capital: float = 250.0    # hard cap on dollars deployed, whatever the equity
    size_by: str = "claude"               # claude: min(Claude's suggestion, cap) | risk: always the cap
    max_core_pct: float = 0.60            # largest size a model may request for itself (trend core)
    swing_min_profit_pct: float = 0.01    # hold a model trade past its exit if up at least this much
    swing_max_days: int = 2               # extra sessions a winner may be held (0 disables)
    model_max_losing_streak: int = 3      # pause a model for the day after this many losses in a row




@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.PAPER
    live_trading_enabled: bool = False
    live_trading_confirm: str = ""
    # The ONLY Robinhood account the bot may ever trade, matched on the full number AND the nickname
    # (other accounts, e.g. an IRA, can share the last 4 digits). Both live in .env, never committed.
    robinhood_account_number: str = field(default="", repr=False)
    robinhood_account_nickname: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    max_claude_reviews_per_day: int = 40  # API cost cap; later candidates are skipped
    max_reviews_per_model_per_day: int = 15  # so one noisy model can't use the whole budget
    starting_capital: float = 250.0
    risk: RiskLimits = RiskLimits()
    ensemble: EnsembleParams = EnsembleParams()
    providers: tuple[str, ...] = ("json_file",)
    strategies_file: Path = Path("config/strategies.toml")
    universe_file: Path = Path("config/universe.txt")
    bars_dir: Path = Path("data/bars")
    # manual: approved entries wait for a click on the dashboard; auto: the bot buys itself.
    # Exits, stops and targets always run automatically.
    entry_mode: str = "manual"
    # t1: sale proceeds usable next business day (cash account, conservative);
    # instant: usable at once (Robinhood margin accounts). MCP is "limited margin".
    paper_settlement: str = "t1"
    market_data: str = "csv"  # csv | robinhood (read-only MCP; run ctbot-robinhood-login first)
    # Public TradingView scanner for earnings dates and review context (robinhood market data only)
    tradingview_data: bool = True
    paper_ledger_path: Path = Path("data/paper_ledger.json")
    paper_risk_state_path: Path = Path("data/paper_risk_state.json")
    log_dir: Path = Path("logs")
    respect_market_hours: bool = True

    @property
    def equity_history_path(self) -> Path:
        """One JSON line per paper cycle; read by the dashboard's equity chart."""
        return self.paper_ledger_path.with_name("equity_history.jsonl")

    @property
    def allocations_path(self) -> Path:  # paper launcher: strategy -> capital (ctbot.allocations)
        return self.paper_ledger_path.with_name("allocations.json")

    @property
    def ideas_path(self) -> Path:
        return self.paper_ledger_path.with_name("ideas.jsonl")

    @property
    def manual_orders_path(self) -> Path:
        """Dashboard clicks, queued for the paper loop (the only process that trades)."""
        return self.paper_ledger_path.with_name("paper_manual_orders.jsonl")

    @property
    def manual_results_path(self) -> Path:
        return self.paper_ledger_path.with_name("paper_manual_results.jsonl")

    @property
    def heartbeat_path(self) -> Path:
        return self.paper_ledger_path.with_name("paper_heartbeat.json")

    @property
    def paper_log_path(self) -> Path:
        return self.log_dir / "ctbot-paper.log"

    @property
    def live_fully_authorized(self) -> bool:
        return (
            self.mode is Mode.LIVE
            and self.live_trading_enabled
            and self.live_trading_confirm == LIVE_CONFIRM_PHRASE
            and bool(self.robinhood_account_number)
            and bool(self.robinhood_account_nickname)
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        if env is None:
            from ctbot.secret_store import load_environment

            load_environment()  # .env, then keys saved from the dashboard's Settings tab
            env = dict(os.environ)

        mode_raw = env.get("CTBOT_MODE", "paper").strip().lower()
        mode = Mode.LIVE if mode_raw == "live" else Mode.PAPER

        risk = RiskLimits(
            max_position_pct=_float(env, "MAX_POSITION_PCT", 0.20),
            max_open_positions=_int(env, "MAX_OPEN_POSITIONS", 3),
            max_daily_loss_pct=_float(env, "MAX_DAILY_LOSS_PCT", 0.03),
            max_total_drawdown_pct=_float(env, "MAX_TOTAL_DRAWDOWN_PCT", 0.15),
            min_share_price=_float(env, "MIN_SHARE_PRICE", 5.0),
            min_avg_dollar_volume=_float(env, "MIN_AVG_DOLLAR_VOLUME", 20_000_000),
            allow_short=_bool(env, "ALLOW_SHORT", False),
            max_day_trades_5d=_int(env, "MAX_DAY_TRADES_5D", 3),
            min_claude_confidence=_float(env, "MIN_CLAUDE_CONFIDENCE", 0.65),
            max_spread_pct=_float(env, "MAX_SPREAD_PCT", 0.01),
            entry_limit_slack_pct=_float(env, "ENTRY_LIMIT_SLACK_PCT", 0.002),
            default_stop_loss_pct=_float(env, "DEFAULT_STOP_LOSS_PCT", 0.05),
            max_stop_loss_pct=_float(env, "MAX_STOP_LOSS_PCT", 0.10),
            no_entries_last_minutes=_float(env, "NO_ENTRIES_LAST_MINUTES", 15.0),
            max_trading_capital=_float(env, "MAX_TRADING_CAPITAL", 250.0),
            size_by=env.get("SIZE_BY", "claude").strip().lower(),
            max_core_pct=_float(env, "MAX_CORE_PCT", 0.60),
            swing_min_profit_pct=_float(env, "SWING_MIN_PROFIT_PCT", 0.01),
            swing_max_days=_int(env, "SWING_MAX_DAYS", 2),
            model_max_losing_streak=_int(env, "MODEL_MAX_LOSING_STREAK", 3),
        )
        ensemble = EnsembleParams(
            min_long_score=_float(env, "MIN_LONG_SCORE", 0.15),
            min_exit_score=_float(env, "MIN_EXIT_SCORE", 0.10),
            min_agreeing_strategies=_int(env, "MIN_AGREEING_STRATEGIES", 2),
            max_candidates=_int(env, "MAX_CANDIDATES_PER_CYCLE", 5),
        )
        providers = tuple(
            p.strip() for p in env.get("ALT_DATA_PROVIDERS", "json_file").split(",") if p.strip()
        )
        return cls(
            mode=mode,
            live_trading_enabled=_bool(env, "LIVE_TRADING_ENABLED", False),
            live_trading_confirm=env.get("LIVE_TRADING_CONFIRM", ""),
            robinhood_account_number=env.get("ROBINHOOD_ACCOUNT_NUMBER", "").strip(),
            robinhood_account_nickname=env.get("ROBINHOOD_ACCOUNT_NICKNAME", "").strip(),
            claude_model=env.get("CLAUDE_MODEL", "claude-opus-5-5"),
            claude_effort=env.get("CLAUDE_EFFORT", "medium"),
            max_claude_reviews_per_day=_int(env, "MAX_CLAUDE_REVIEWS_PER_DAY", 40),
            max_reviews_per_model_per_day=_int(env, "MAX_REVIEWS_PER_MODEL_PER_DAY", 15),
            starting_capital=_float(env, "STARTING_CAPITAL", 250.0),
            risk=risk,
            ensemble=ensemble,
            providers=providers,
            strategies_file=Path(env.get("STRATEGIES_FILE", "config/strategies.toml")),
            universe_file=Path(env.get("UNIVERSE_FILE", "config/universe.txt")),
            bars_dir=Path(env.get("BARS_DIR", "data/bars")),
            market_data=env.get("MARKET_DATA", "csv").strip().lower(),
            tradingview_data=_bool(env, "TRADINGVIEW_DATA", True),
            entry_mode=env.get("ENTRY_MODE", "manual").strip().lower(),
            paper_settlement=env.get("PAPER_SETTLEMENT", "t1").strip().lower(),
            paper_ledger_path=Path(env.get("PAPER_LEDGER_PATH", "data/paper_ledger.json")),
            paper_risk_state_path=Path(
                env.get("PAPER_RISK_STATE_PATH", "data/paper_risk_state.json")
            ),
            log_dir=Path(env.get("LOG_DIR", "logs")),
            respect_market_hours=_bool(env, "RESPECT_MARKET_HOURS", True),
        )


def load_universe(path: Path) -> list[str]:
    lines = path.read_text().splitlines()
    return [ln.strip().upper() for ln in lines if ln.strip() and not ln.startswith("#")]
