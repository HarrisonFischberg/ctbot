"""Market-wide "stocks in play" scan via Robinhood's scanner (preview_scan: read-only, nothing
is saved to the account). Ranks Russell 1000 names by today's volume so far relative to
their 14-day average daily volume; early in the session that ranks unusual opening activity,
the selection step of Zarattini, Barbon & Aziz (2024) ORB on Stocks in Play.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

from ctbot import market_hours

log = logging.getLogger(__name__)

CallMany = Callable[[list[tuple[str, dict[str, Any]]]], list[dict[str, Any]]]
RVOL = 'dayVolume / volumeAvg(candleCount=14, candlePeriod="1d", session="regular")'
ATR = 'atr(candleCount=14, candlePeriod="1d", session="regular")'


def scan_filters(min_price: float, min_avg_volume: float, min_rvol_ratio: float, index: str) -> list[dict]:
    return [
        {"expression": "symbol", "predicate": "PREDICATE_IN_LIST", "values": [index]},
        {"expression": f"tradeAllDay.price > {min_price}", "predicate": "PREDICATE_EQUAL", "values": ["True"]},
        {"expression": 'volumeAvg(candleCount=14, candlePeriod="1d", session="regular")',
         "predicate": "PREDICATE_GREATER_THAN", "values": [str(int(min_avg_volume))], "display_title": "AvgVol14"},
        {"expression": RVOL, "predicate": "PREDICATE_GREATER_THAN_OR_EQUAL", "values": [str(min_rvol_ratio)],
         "display_title": "RVOL"},
    ]


def parse_scan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for r in ((payload.get("data") or {}).get("result") or {}).get("results", []):
        cols = r.get("columns") or {}
        try:
            rows.append({"symbol": r["ticker"].upper(), "rvol_ratio": float(cols["RVOL"]),
                         "atr14": float(cols["ATR14"]), "last": float(cols.get("Last") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(rows, key=lambda x: x["rvol_ratio"], reverse=True)


class StocksInPlayScanner:
    """One scan per trading day, taken once the opening bar has formed; cached thereafter."""

    def __init__(self, call_many: CallMany, top_k: int = 20, min_price: float = 5.0,
                 min_avg_volume: float = 1_000_000, min_rvol_ratio: float = 0.04,
                 index: str = "Russell1000",
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._call_many = call_many
        self.top_k, self.min_price, self.min_avg_volume = top_k, min_price, min_avg_volume
        self.min_rvol_ratio, self.index, self.clock = min_rvol_ratio, index, clock
        self._day: date | None = None
        self._rows: list[dict[str, Any]] = []

    def today(self) -> list[dict[str, Any]]:
        now = self.clock()
        d = market_hours.trading_date(now)
        if self._day == d:
            return self._rows
        et = now.astimezone(market_hours.ET)
        if not market_hours.session_open(now) or (et.hour, et.minute) < (9, 36):
            return []  # wait for the first 5-minute bar to complete
        try:
            (payload,) = self._call_many([("preview_scan", {
                "filters": scan_filters(self.min_price, self.min_avg_volume, self.min_rvol_ratio, self.index),
                "columns": [{"display_name": "ATR14", "expression": ATR}],
            })])
            rows = parse_scan(payload)[: self.top_k]
        except Exception:  # noqa: BLE001 - retried next cycle
            log.exception("stocks-in-play scan failed")
            return []
        self._day, self._rows = d, rows
        log.info("stocks in play %s: %s", d, ", ".join(f"{r['symbol']}({r['rvol_ratio']:.2f})" for r in rows))
        return rows
