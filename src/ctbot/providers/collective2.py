"""Collective2 (C2) read-only client and copy-trading provider.

Only the endpoints in READ_ENDPOINTS can be called; anything else (subscriptions, autotrade,
order or position-setting endpoints) is refused before a request is sent. C2 has no sandbox,
so this allowlist is the guard. The API key comes from C2_API_KEY and is never logged.

What a free account can see (verified 2026-10-14):
  * strategy search, details, monthly and daily equity, and CLOSED trades: yes
  * a strategy's OPEN positions: no (403) unless you subscribe to that strategy
So without a subscription the provider can only report exits (from closed trades); entries
appear only for strategies you subscribe to. C2's terms forbid redistributing paid content:
keep fetched data local.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ctbot.models import AltDataEvent, EventKind, Side
from ctbot.providers.base import ProviderNotConfigured

log = logging.getLogger(__name__)

BASE_URL = "https://api4-general.collective2.com"
READ_ENDPOINTS = frozenset(
    {
        ("POST", "/General/Hello"),
        ("GET", "/Strategies/SearchStrategies"),
        ("GET", "/Strategies/GetLeaderBoard"),
        ("GET", "/Strategies/GetStrategyDetails"),
        ("GET", "/Strategies/GetStrategyHistoricalEquity"),
        ("GET", "/Strategies/GetStrategyHistoricalDailyEquity"),
        ("GET", "/Strategies/GetStrategyHistoricalClosedTrades"),
        ("GET", "/Strategies/GetStrategyOpenPositions"),
    }
)


class EndpointNotAllowed(PermissionError):
    pass


class C2Forbidden(RuntimeError):
    """403: usually means the data needs a subscription to that strategy."""


class C2Client:
    def __init__(self, api_key: str | None, http=None, max_retries: int = 4) -> None:
        if not api_key:
            raise ProviderNotConfigured("C2_API_KEY is not set")
        import httpx

        self._http = http or httpx.Client(base_url=BASE_URL, timeout=60)
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self.max_retries = max_retries

    def request(self, method: str, path: str, params: dict | None = None,
                json_body: dict | None = None) -> dict[str, Any]:
        if (method, path) not in READ_ENDPOINTS:
            raise EndpointNotAllowed(f"{method} {path} is not an allowed read-only endpoint")
        headers = dict(self._headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            r = self._http.request(method, path, params=params, json=json_body, headers=headers)
            if r.status_code == 429 and attempt < self.max_retries:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                continue
            if r.status_code == 403:
                raise C2Forbidden(f"{path} forbidden (subscription required?)")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"{path}: rate limited after {self.max_retries} retries")

    def get(self, path: str, **params) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def paged(self, path: str, **params) -> list[dict[str, Any]]:
        out, cursor = [], None
        while True:
            q = {**params, "Limit": 1000, **({"Cursor": cursor} if cursor else {})}
            data = self.get(path, **q)
            out += data.get("Results") or []
            cursor = (data.get("Pagination") or {}).get("next_cursor")
            if not cursor:
                return out

    # ---- convenience ---------------------------------------------------------
    def search_stocks(self) -> list[dict[str, Any]]:
        return self.paged("/Strategies/SearchStrategies", SecurityType="CS")

    def details(self, sid: int) -> dict[str, Any]:
        res = self.get("/Strategies/GetStrategyDetails", StrategyId=sid).get("Results") or [{}]
        return res[0] if isinstance(res, list) else res

    def daily_equity(self, sid: int) -> list[dict[str, Any]]:
        res = self.get("/Strategies/GetStrategyHistoricalDailyEquity", StrategyId=sid,
                       CommissionPlan=0).get("Results") or []
        return (res[0] or {}).get("DailyEquity") or [] if res else []

    def closed_trades(self, sid: int) -> list[dict[str, Any]]:
        return self.get("/Strategies/GetStrategyHistoricalClosedTrades", StrategyId=sid,
                        CommissionPlan=0).get("Results") or []

    def open_positions(self, sid: int) -> list[dict[str, Any]]:
        return self.get("/Strategies/GetStrategyOpenPositions", StrategyIds=sid,
                        SecurityType="CS").get("Results") or []


def _d(s: str) -> date:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date()


def _symbol(t: dict[str, Any]) -> str | None:
    sym = t.get("ExchangeSymbol") or t.get("C2Symbol") or {}
    if isinstance(sym, dict):
        sym = sym.get("Symbol") or sym.get("FullSymbol")
    return str(sym).upper() if sym else None


class Collective2Provider:
    """Events for the copy_trading strategy; actor = "c2:<StrategyId>".

    * open long positions (subscribed strategies only) -> BUY events
    * long trades closed in the last `exit_lookback_days` -> SELL events (exit if we hold it)
    * shorts are ignored (the account is long-only)
    """

    name = "collective2"
    supports = frozenset({EventKind.INVESTOR_TRADE})

    def __init__(self, api_key: str | None, strategy_ids: list[int], client: C2Client | None = None,
                 exit_lookback_days: int = 5) -> None:
        self.client = client or C2Client(api_key)
        self.strategy_ids = strategy_ids
        self.exit_lookback_days = exit_lookback_days
        self._warned: set[int] = set()

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        if EventKind.INVESTOR_TRADE not in kinds:
            return []
        today = datetime.now(timezone.utc).date()
        out: list[AltDataEvent] = []
        for sid in self.strategy_ids:
            actor = f"c2:{sid}"
            try:
                for p in self.client.open_positions(sid):
                    sym = _symbol(p)
                    if sym and float(p.get("Quantity") or 0) > 0 and p.get("OpenedDate"):
                        out.append(AltDataEvent(EventKind.INVESTOR_TRADE, sym, Side.BUY, actor,
                                                _d(p["OpenedDate"]), today, self.name, None,
                                                {"avg_px": p.get("AvgPx"), "strategy": p.get("StrategyName")}))
            except C2Forbidden:
                if sid not in self._warned:
                    self._warned.add(sid)
                    log.warning("C2 strategy %s: open positions need a subscription; following exits only", sid)
            cutoff = today - timedelta(days=self.exit_lookback_days)
            for t in self.client.closed_trades(sid):
                sym, closed = _symbol(t), t.get("CloseDate")
                if sym and closed and str(t.get("OpenSide")) == "1" and _d(closed) >= max(cutoff, since):
                    out.append(AltDataEvent(EventKind.INVESTOR_TRADE, sym, Side.SELL, actor,
                                            _d(closed), _d(closed), self.name, None,
                                            {"close_px": t.get("AvgCloseFillPrice")}))
        return out
