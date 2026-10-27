"""Keys saved from the dashboard's Settings tab, kept outside the project.

Stored in ~/.ctbot/settings.json (file mode 0600, folder 0700), next to the Robinhood sign-in
token, so they can never be committed. Values saved here override .env. The dashboard writes
them but never sends a saved value back to the browser: it only reports whether each one is set.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

STORE_PATH = Path.home() / ".ctbot" / "settings.json"

# Only these names can be saved, each with the shape its value must have.
KEYS: dict[str, tuple[str, re.Pattern[str]]] = {
    "ANTHROPIC_API_KEY": ("Anthropic API key", re.compile(r"[A-Za-z0-9_\-]{20,200}")),
    "OPENAI_API_KEY": ("OpenAI API key (optional, Bub's fallback)", re.compile(r"[A-Za-z0-9_\-]{20,300}")),
    "C2_API_KEY": ("Collective2 API key", re.compile(r"[A-Za-z0-9_\-]{8,200}")),
    "MARKETAUX_API_TOKEN": ("Marketaux news API token (optional)", re.compile(r"[A-Za-z0-9_\-]{16,200}")),
    "TRADINGFLOW_API_KEY": ("TradingFlow API key (optional)", re.compile(r"[A-Za-z0-9_\-.]{16,300}")),
    "WEBULL_APP_KEY": ("Webull App Key (optional)", re.compile(r"[A-Za-z0-9_\-]{16,128}")),
    "WEBULL_APP_SECRET": ("Webull App Secret (optional)", re.compile(r"[A-Za-z0-9_\-]{16,128}")),
    "ROBINHOOD_ACCOUNT_NUMBER": ("Robinhood account number", re.compile(r"[A-Za-z0-9]{4,32}")),
    "ROBINHOOD_ACCOUNT_NICKNAME": ("Robinhood account nickname", re.compile(r"[\w .'\-]{1,50}")),
}


def load(path: Path | None = None) -> dict[str, str]:
    path = path or STORE_PATH  # read at call time, so tests can point it elsewhere
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if k in KEYS and isinstance(v, str) and v}


def save(key: str, value: str, path: Path | None = None) -> None:
    """Set one key; an empty value removes it. Raises ValueError for unknown keys or bad values."""
    if key not in KEYS:
        raise ValueError("unknown setting")
    value = value.strip()
    label, pattern = KEYS[key]
    if value and not pattern.fullmatch(value):
        raise ValueError(f"that does not look like a {label}")
    path = path or STORE_PATH
    data = load(path)
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


_seen: dict = {"stamp": None, "keys": set()}  # what this process last applied


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def apply_to_environ(path: Path | None = None) -> None:
    """Copy saved keys into this process's environment (overriding .env)."""
    path = path or STORE_PATH
    saved = load(path)
    os.environ.update(saved)
    _seen.update(stamp=_stamp(path), keys=set(saved))


def reload_if_changed(path: Path | None = None) -> bool:
    """Pick up keys saved from the dashboard while this process runs. A key removed there falls
    back to .env (or is unset). Returns True when something changed. Cheap: one stat() call."""
    path = path or STORE_PATH
    stamp = _stamp(path)
    if stamp == _seen["stamp"]:
        return False
    saved = load(path)
    removed = _seen["keys"] - set(saved)
    if removed:
        try:
            from dotenv import dotenv_values, find_dotenv

            fallback = dotenv_values(find_dotenv(usecwd=True))
        except ImportError:
            fallback = {}
        for key in removed:
            if fallback.get(key):
                os.environ[key] = fallback[key]
            else:
                os.environ.pop(key, None)
    os.environ.update(saved)
    _seen.update(stamp=stamp, keys=set(saved))
    return True


def load_environment(path: Path | None = None) -> None:
    """.env first, then the Settings-tab values and the assistant's tuning on top. Call before
    reading settings."""
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass
    apply_to_environ(path)
    from ctbot import tuning

    tuning.apply_to_environ(None if path is None else path.with_name("tuning.json"))


def status(path: Path | None = None, env: dict[str, str] | None = None) -> dict[str, dict]:
    """Where each key comes from, without its value: "settings", ".env" or None."""
    env = os.environ if env is None else env
    saved = load(path)
    out = {}
    for key, (label, _) in KEYS.items():
        source = "settings" if key in saved else (".env" if env.get(key) else None)
        out[key] = {"label": label, "set": source is not None, "source": source}
    return out
