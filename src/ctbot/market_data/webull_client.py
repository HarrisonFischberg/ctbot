"""Read-only client for Webull's OpenAPI (https://developer.webull.com), market data only.

CTbot uses Webull as a second source of candles: a backup for Robinhood's completed daily bars
(market_data/failover.py) and for the dashboard's candlestick charts. The allowlist below holds
only market-data and sign-in paths, so no trading endpoint (orders, accounts) can be reached,
whatever a caller asks for.

Auth is an App Key and App Secret from Webull's "OpenAPI Management > App Management" page:
WEBULL_APP_KEY and WEBULL_APP_SECRET, from .env or the dashboard's Settings tab, read at call
time so a key saved on the dashboard applies without a restart. Each request is signed with
HMAC (Webull's "Signature" doc); the secret itself is never sent. When the Webull app has
two-factor sign-in on, Webull also wants an access token that the user approves once in the
Webull mobile app. It is kept in ~/.ctbot/webull_token.json (0600) and reused until Webull
drops it (15 days without a call). Keys and the token are never logged or put in an error.

US stock data needs an OpenAPI market-data subscription on the Webull account (Nasdaq Basic
"non-display"); a subscription bought in the Webull app does not carry over. Without one Webull
answers with an error, which surfaces here as SourceUnavailable with Webull's message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ctbot.models import Bar
from ctbot.net import USER_AGENT, SourceUnavailable

log = logging.getLogger(__name__)

KEY_ENV, SECRET_ENV, HOST_ENV = "WEBULL_APP_KEY", "WEBULL_APP_SECRET", "WEBULL_API_HOST"
HOSTS = frozenset({"api.webull.com", "api.sandbox.webull.com"})  # production, Webull's sandbox
TOKEN_PATH = Path.home() / ".ctbot" / "webull_token.json"
BARS_PER_CALL = 20  # Webull's cap on symbols per bars request

# Only these (method, path) pairs can be sent. No trading or account path is listed.
READ_ONLY = frozenset({
    ("GET", "/openapi/config"),              # is a 2FA token needed?
    ("POST", "/auth/tokens/create"),         # 2FA token: ask for one (approved in the Webull app)
    ("POST", "/auth/tokens/check"),          # 2FA token: has it been approved yet?
    ("GET", "/market-data/stocks/snapshots/list"),
    ("POST", "/market-data/stocks/bars/list"),
})
TIMESPANS = {"5minute": "M5", "hour": "M60", "day": "D", "week": "W"}
SIGNERS = {"HMAC-SHA1": (hashlib.sha1, hashlib.md5), "HMAC-SHA256": (hashlib.sha256, hashlib.sha256)}


class PathNotAllowed(PermissionError):
    pass


def check_allowed(method: str, path: str) -> None:
    if (method, path) not in READ_ONLY:
        raise PathNotAllowed(f"{method} {path} is not an allowed Webull path; refusing to call it")


def string_to_sign(path: str, params: dict[str, str], body: str | None, digest=hashlib.md5) -> str:
    """Webull's signing string: path & sorted name=value pairs (query + signed headers) & the
    body's uppercase hex digest, URL-encoded with nothing left safe."""
    pairs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    s = f"{path}&{pairs}"
    if body:
        s += "&" + digest(body.encode()).hexdigest().upper()
    return quote(s, safe="")


def sign(secret: str, encoded: str, algorithm: str = "HMAC-SHA256") -> str:
    mac = hmac.new((secret + "&").encode(), encoded.encode(), SIGNERS[algorithm][0])
    return base64.b64encode(mac.digest()).decode()


def _ts(v: Any) -> datetime:
    if isinstance(v, (int, float)) or str(v).isdigit():
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(str(v)).astimezone(timezone.utc)


def parse_bars(payload: Any) -> dict[str, list[Bar]]:
    """{"result": [{"symbol", "result": [{time, open, high, low, close, volume}]}]}, oldest first."""
    groups = payload.get("result", []) if isinstance(payload, dict) else payload or []
    out: dict[str, list[Bar]] = {}
    for g in groups:
        sym = str(g.get("symbol", "")).upper()
        bars = []
        for b in g.get("result") or g.get("bars") or []:
            try:
                o, h, lo, c = (float(b[k]) for k in ("open", "high", "low", "close"))
                bars.append(Bar(sym, _ts(b["time"]), o, h, lo, c, float(b.get("volume") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        if sym:
            out[sym] = sorted((b for b in bars if min(b.open, b.high, b.low, b.close) > 0), key=lambda b: b.ts)
    return out


class WebullClient:
    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        host: str | None = None,
        token_path: Path | None = None,
        algorithm: str = "HMAC-SHA256",
        http: httpx.Client | None = None,
        failure_threshold: int = 3,
        cooldown_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app_key, self._app_secret, self._host = app_key, app_secret, host
        self._token_path = token_path
        self.algorithm = algorithm
        self._http = http or httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    # ---- configuration (read at call time) -----------------------------------
    @property
    def app_key(self) -> str:
        return self._app_key if self._app_key is not None else os.environ.get(KEY_ENV, "").strip()

    @property
    def app_secret(self) -> str:
        return self._app_secret if self._app_secret is not None else os.environ.get(SECRET_ENV, "").strip()

    @property
    def host(self) -> str:
        host = self._host or os.environ.get(HOST_ENV, "").strip() or "api.webull.com"
        if host not in HOSTS:  # the signed key only ever goes to Webull
            raise SourceUnavailable(f"webull: {HOST_ENV} must be one of {', '.join(sorted(HOSTS))}")
        return host

    @property
    def token_path(self) -> Path:
        return self._token_path or TOKEN_PATH

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    @property
    def available(self) -> bool:
        return self.configured and self._clock() >= self._open_until

    # ---- requests ---------------------------------------------------------
    def _redact(self, text: str) -> str:
        for s in (self.app_key, self.app_secret, (self.saved_token() or {}).get("token")):
            if s:
                text = text.replace(s, "REDACTED")
        return text

    def request(self, method: str, path: str, query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> Any:
        check_allowed(method, path)
        key, secret = self.app_key, self.app_secret
        if not (key and secret):
            raise SourceUnavailable(f"webull: {KEY_ENV} and {SECRET_ENV} are not set")
        host = self.host
        query = {k: str(v) for k, v in (query or {}).items() if v is not None}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else None
        signed = {
            "x-app-key": key,
            "x-signature-algorithm": self.algorithm,
            "x-signature-version": "1.0",
            "x-signature-nonce": uuid.uuid4().hex,
            "x-timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        encoded = string_to_sign(path, {**query, **signed, "host": host}, raw, SIGNERS[self.algorithm][1])
        headers = {**signed, "x-version": "v3", "x-signature": sign(secret, encoded, self.algorithm)}
        tok = self.saved_token()
        if tok and tok.get("status") == "NORMAL" and not path.startswith("/auth/"):
            headers["x-access-token"] = tok["token"]
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            r = self._http.request(method, f"https://{host}{path}", params=query or None,
                                   content=raw.encode() if raw is not None else None, headers=headers)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"webull: {type(e).__name__}") from None  # never str(e): has the URL
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = j.get("message") or j.get("msg") or j.get("error_code") or ""
            except (ValueError, AttributeError):
                msg = ""
            raise SourceUnavailable(self._redact(f"webull: HTTP {r.status_code} {str(msg)[:160]}".strip()))
        try:
            return r.json()
        except ValueError:
            raise SourceUnavailable("webull: response was not JSON") from None

    def _market(self, method: str, path: str, **kw: Any) -> Any:
        """Market-data call behind a circuit breaker: an outage costs one log line."""
        if self.configured and self._clock() < self._open_until:
            raise SourceUnavailable("webull: cooling down after repeated failures")
        try:
            out = self.request(method, path, **kw)
        except SourceUnavailable as e:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open_until, self._failures = self._clock() + self.cooldown_s, 0
                log.warning("%s; pausing Webull for %.0f min", e, self.cooldown_s / 60)
            raise
        self._failures = 0
        return out

    # ---- market data ------------------------------------------------------
    def bars(self, symbols: list[str], interval: str, count: int = 200) -> dict[str, list[Bar]]:
        """Regular-session candles (interval: 5minute, hour, day, week), oldest first.
        Daily and longer bars are split-adjusted by Webull; minute bars are not."""
        syms = sorted({s.upper() for s in symbols})
        out: dict[str, list[Bar]] = {}
        for i in range(0, len(syms), BARS_PER_CALL):
            body = {"symbols": syms[i:i + BARS_PER_CALL], "category": "US_STOCK",
                    "timespan": TIMESPANS[interval], "count": max(1, min(int(count), 1200))}
            out.update(parse_bars(self._market("POST", "/market-data/stocks/bars/list", body=body)))
        return out

    def snapshot(self, symbols: list[str]) -> list[dict[str, Any]]:
        res = self._market("GET", "/market-data/stocks/snapshots/list",
                           query={"symbols": ",".join(s.upper() for s in symbols), "category": "US_STOCK"})
        return res if isinstance(res, list) else res.get("result", [])

    # ---- two-factor token -------------------------------------------------
    def token_required(self) -> bool:
        return bool(self.request("GET", "/openapi/config").get("token_check_enabled"))

    def saved_token(self) -> dict[str, Any] | None:
        try:
            t = json.loads(self.token_path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        return t if isinstance(t, dict) and t.get("token") else None

    def _save_token(self, t: dict[str, Any]) -> None:
        path = self.token_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: t.get(k) for k in ("token", "expires", "status")}, f)
        os.replace(tmp, path)

    def create_token(self) -> str:
        """Ask Webull for a token (or re-validate the saved one). Webull texts a code to the
        account's phone; the user enters it in the Webull app within 5 minutes. Returns status."""
        old = (self.saved_token() or {}).get("token")
        t = self.request("POST", "/auth/tokens/create", body={"token": old} if old else {})
        if not isinstance(t, dict) or not t.get("token"):
            raise SourceUnavailable("webull: no token in the reply")
        self._save_token(t)
        return str(t.get("status"))

    def check_token(self) -> str | None:
        """Refresh the saved token's status: PENDING, NORMAL, INVALID or EXPIRED (None if none)."""
        saved = self.saved_token()
        if not saved:
            return None
        t = self.request("POST", "/auth/tokens/check", body={"token": saved["token"]})
        status = t.get("status") if isinstance(t, dict) else None
        if status:
            self._save_token({**saved, **{k: v for k, v in t.items() if k in ("status", "expires")}})
        return status or saved.get("status")

    def forget_token(self) -> None:
        self.token_path.unlink(missing_ok=True)
