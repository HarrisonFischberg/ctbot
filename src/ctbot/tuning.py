"""Risk and ensemble knobs the dashboard assistant may change, kept outside the project.

Stored in ~/.ctbot/tuning.json (file mode 0600, folder 0700), next to secret_store's
settings.json and written the same atomic way. Values here override .env; load_environment()
applies them after .env. The running paper bot polls reload_if_changed() and rebuilds its
risk limits and ensemble from them (see `overlay`), so a change applies without a restart.

Only the knobs in KNOBS can be stored, each inside the bounds written below. DENYLIST names the
settings no tuning path may ever touch (live trading, the account, the money and loss caps); it
is checked before the allowlist, so widening KNOBS by mistake still cannot reach them.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

TUNING_PATH = Path.home() / ".ctbot" / "tuning.json"

# Never tunable, whatever KNOBS says: live trading, the one account the bot may trade, and the
# hard money / loss / stop caps. The assistant's own message cap is here so it can't raise it.
DENYLIST = frozenset({
    "CTBOT_MODE", "LIVE_TRADING_ENABLED", "LIVE_TRADING_CONFIRM",
    "ROBINHOOD_ACCOUNT_NUMBER", "ROBINHOOD_ACCOUNT_NICKNAME",
    "MAX_TRADING_CAPITAL", "STARTING_CAPITAL", "MAX_DAILY_LOSS_PCT", "MAX_TOTAL_DRAWDOWN_PCT",
    "DEFAULT_STOP_LOSS_PCT", "MAX_STOP_LOSS_PCT", "ALLOW_SHORT", "MAX_DAY_TRADES_5D",
    "MAX_ASSISTANT_MESSAGES_PER_DAY",
})
DENIED_PREFIXES = ("LIVE_", "ROBINHOOD_", "ANTHROPIC_")


@dataclass(frozen=True)
class Knob:
    label: str
    kind: str                 # "float" | "int" | "choice"
    field: str                # "risk.<name>", "ensemble.<name>" or a Settings attribute
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    pct: bool = False         # shown as a percentage
    looser: str = "lower"     # which way trades more: "lower" or "higher" ("" for choices)


# Bounds: loose enough to trade noticeably more (or less) in paper, never past the hard caps.
KNOBS: dict[str, Knob] = {
    "MIN_CLAUDE_CONFIDENCE": Knob("Min Claude confidence", "float", "risk.min_claude_confidence", 0.50, 0.90),
    "MIN_LONG_SCORE": Knob("Min ensemble score", "float", "ensemble.min_long_score", 0.05, 0.50),
    "MIN_AGREEING_STRATEGIES": Knob("Strategies that must agree", "int", "ensemble.min_agreeing_strategies", 1, 4),
    "MAX_CANDIDATES_PER_CYCLE": Knob("Ideas reviewed per cycle", "int", "ensemble.max_candidates", 1, 10, looser="higher"),
    "MAX_OPEN_POSITIONS": Knob("Max open positions", "int", "risk.max_open_positions", 1, 8, looser="higher"),
    "MAX_CLAUDE_REVIEWS_PER_DAY": Knob("Claude reviews per day", "int", "max_claude_reviews_per_day", 5, 100, looser="higher"),
    "MAX_REVIEWS_PER_MODEL_PER_DAY": Knob("Claude reviews per model per day", "int", "max_reviews_per_model_per_day", 1, 50, looser="higher"),
    "MAX_SPREAD_PCT": Knob("Max bid/ask spread", "float", "risk.max_spread_pct", 0.002, 0.02, pct=True, looser="higher"),
    "NO_ENTRIES_LAST_MINUTES": Knob("No new buys in the last N minutes", "float", "risk.no_entries_last_minutes", 5, 60),
    "MAX_POSITION_PCT": Knob("Max size of one position", "float", "risk.max_position_pct", 0.05, 0.35, pct=True, looser="higher"),
    "ENTRY_MODE": Knob("Entry mode", "choice", "entry_mode", choices=("manual", "auto"), looser=""),
}


def denied(key: str) -> bool:
    return key in DENYLIST or key.startswith(DENIED_PREFIXES)


def validate(key: str, value: Any) -> int | float | str:
    """The stored form of `value`, or ValueError. The denylist is checked first."""
    if not isinstance(key, str) or denied(key):
        raise ValueError(f"{key} can never be changed from the dashboard assistant")
    knob = KNOBS.get(key)
    if knob is None:
        raise ValueError(f"{key} is not a setting the assistant may change")
    if knob.kind == "choice":
        v = str(value).strip().lower()
        if v not in knob.choices:
            raise ValueError(f"{knob.label} must be one of {', '.join(knob.choices)}")
        return v
    if isinstance(value, bool):
        raise ValueError(f"{knob.label} must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{knob.label} must be a number") from None
    if math.isnan(num) or not knob.lo <= num <= knob.hi:
        raise ValueError(f"{knob.label} must be between {fmt(key, knob.lo)} and {fmt(key, knob.hi)}")
    if knob.kind == "int":
        if num != int(num):
            raise ValueError(f"{knob.label} must be a whole number")
        return int(num)
    return round(num, 4)


def fmt(key: str, value: Any) -> str:
    knob = KNOBS.get(key)
    if value is None:
        return "default"
    if knob and knob.pct and isinstance(value, (int, float)):
        return f"{value * 100:g}%"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def current(settings, key: str) -> Any:
    """The value `settings` uses now for one knob."""
    obj: Any = settings
    for part in KNOBS[key].field.split("."):
        obj = getattr(obj, part)
    return obj


def load(path: Path | None = None) -> dict[str, Any]:
    """Saved knobs; anything unknown, denied or out of bounds (a hand edit) is ignored."""
    path = path or TUNING_PATH
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out = {}
    for k, v in (data.items() if isinstance(data, dict) else ()):
        try:
            out[k] = validate(k, v)
        except ValueError:
            continue
    return out


def save(key: str, value: Any, path: Path | None = None) -> None:
    """Set one knob; None removes it (back to .env or the default). Raises ValueError."""
    path = path or TUNING_PATH
    data = load(path)
    if value is None:
        if denied(key) or key not in KNOBS:
            raise ValueError(f"{key} is not a setting the assistant may change")
        data.pop(key, None)
    else:
        data[key] = validate(key, value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _dotenv(key: str) -> str | None:
    try:
        from dotenv import dotenv_values, find_dotenv

        return dotenv_values(find_dotenv(usecwd=True)).get(key) or None
    except ImportError:
        return None


def fallback(key: str) -> tuple[Any, str]:
    """What one knob becomes once removed from tuning.json: (value, ".env" or "default")."""
    from ctbot.config import Settings

    env = {k: v for k, v in os.environ.items() if k != key}
    dv = _dotenv(key)
    if dv:
        env[key] = dv
    return current(Settings.from_env(env), key), ".env" if dv else "default"


def _env_fallback(key: str) -> None:
    fallback = _dotenv(key)
    if fallback:
        os.environ[key] = fallback
    else:
        os.environ.pop(key, None)


_seen: dict = {"stamp": None, "keys": set()}  # what this process last applied


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def apply_to_environ(path: Path | None = None) -> None:
    """Copy saved knobs into this process's environment (overriding .env)."""
    path = path or TUNING_PATH
    saved = load(path)
    os.environ.update({k: str(v) for k, v in saved.items()})
    _seen.update(stamp=_stamp(path), keys=set(saved))


def reload_if_changed(path: Path | None = None) -> bool:
    """Pick up knobs changed from the dashboard while this process runs. A removed knob falls
    back to .env (or the default). Returns True when something changed. One stat() call."""
    path = path or TUNING_PATH
    stamp = _stamp(path)
    if stamp == _seen["stamp"]:
        return False
    saved = load(path)
    for key in _seen["keys"] - set(saved):
        _env_fallback(key)
    os.environ.update({k: str(v) for k, v in saved.items()})
    _seen.update(stamp=stamp, keys=set(saved))
    return True


def overlay(settings, env: dict[str, str] | None = None):
    """`settings` with only the tunable fields re-read from `env` (default os.environ). Every
    other field, live flags and money caps included, is kept exactly as it was."""
    from ctbot.config import Settings

    fresh = Settings.from_env(dict(os.environ if env is None else env))
    risk, ens, top = {}, {}, {}
    for knob in KNOBS.values():
        group, _, name = knob.field.rpartition(".")
        src = getattr(fresh, group) if group else fresh
        {"risk": risk, "ensemble": ens, "": top}[group][name] = getattr(src, name)
    return replace(settings, risk=replace(settings.risk, **risk),
                   ensemble=replace(settings.ensemble, **ens), **top)
