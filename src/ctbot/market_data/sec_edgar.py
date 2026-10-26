"""SEC EDGAR filings and reported numbers for reviews (free, public; needs SEC_USER_AGENT).

Two things from the day-trader pack (github.com/Jakeschincariol/day-trader-skill, MIT):

* **8-K items** (trader-news): each company's recent 8-Ks from data.sec.gov's submissions
  JSON, as item codes only (2.02 earnings results, 5.02 officer change, ...). Three items are
  red flags that block new entries for RED_FLAG_DAYS: bankruptcy (1.03), a delisting notice
  (3.01) and past financials no longer reliable (4.02).
* **Reported numbers** (trader-analyst): the latest quarter from the XBRL company facts the
  company filed (10-Q/10-K): revenue, net income and diluted EPS against the same quarter a
  year earlier, and gross, operating and net margins. A fourth quarter is the 10-K year minus
  its three quarters and is marked derived.

Only codes and numbers reach Claude, never filing text. SEC asks every client to identify
itself ("Your Name you@example.com"); without SEC_USER_AGENT nothing here runs. SEC allows
10 requests a second; this makes at most a few per review and caches each answer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ctbot.net import HttpSource, SourceUnavailable

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = frozenset({"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"})
RED_FLAG_ITEMS = frozenset({"1.03", "3.01", "4.02"})
RED_FLAG_DAYS = 14

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FORMS = ("10-Q", "10-K", "20-F", "40-F")


def valid_user_agent(ua: str | None) -> bool:
    """SEC wants a name and an email; the pack's placeholder address doesn't count."""
    ua = (ua or "").strip()
    return "@" in ua and "you@example.com" not in ua


# ---- 8-K items --------------------------------------------------------------------------

def recent_8k(submissions: dict[str, Any], today: date, days: int = 7) -> list[dict[str, Any]]:
    """8-K and 8-K/A filings of the last `days` days, newest first: date and item codes."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms, dates, items = recent.get("form") or [], recent.get("filingDate") or [], recent.get("items") or []
    since = today - timedelta(days=days)
    out = []
    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A") or i >= len(dates):
            continue
        try:
            d = date.fromisoformat(dates[i])
        except ValueError:
            continue
        if d < since or d > today:
            continue
        codes = [c.strip() for c in (items[i] if i < len(items) else "").split(",") if c.strip()]
        out.append({"date": d.isoformat(), "form": form, "items": [c for c in codes if c != "9.01"]})
    out.sort(key=lambda f: f["date"], reverse=True)
    return out


def summarize_8k(filings: list[dict[str, Any]], today: date) -> dict[str, Any]:
    codes = sorted({c for f in filings for c in f["items"]})
    flags = sorted({c for f in filings for c in f["items"] if c in RED_FLAG_ITEMS
                    and (today - date.fromisoformat(f["date"])).days <= RED_FLAG_DAYS})
    return {
        "source": "sec_edgar", "filings": len(filings),
        "latest": filings[0]["date"] if filings else None,
        "important": [f"{c} {ITEMS_8K[c]}" for c in codes if c in IMPORTANT_ITEMS],
        "other": [f"{c} {ITEMS_8K.get(c, 'item')}" for c in codes if c not in IMPORTANT_ITEMS],
        "red_flags": [f"{c} {ITEMS_8K[c]}" for c in flags],
    }


# ---- reported numbers -------------------------------------------------------------------

def _facts(companyfacts: dict[str, Any], concept: str, unit: str) -> list[dict[str, Any]]:
    node = ((companyfacts.get("facts") or {}).get("us-gaap") or {}).get(concept) or {}
    units = node.get("units") or {}
    return units.get(unit) or (next(iter(units.values())) if units else [])


def _quarters(facts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """end date -> three-month value from 10-Q/10-K facts; the latest filing wins (restatements).
    A missing fourth quarter is derived as the year minus its three quarters."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        if "start" not in f or str(f.get("form", "")).split("/")[0] not in FORMS:
            continue
        key = (f["start"], f["end"])
        if key not in latest or (f.get("filed", ""), f.get("accn", "")) > (
                latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
    q: dict[str, dict[str, Any]] = {}
    years = []
    for (start, end), f in latest.items():
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if 80 <= days <= 100:
            q[end] = {"value": f["val"], "start": start, "derived": False, "filed": f.get("filed")}
        elif 350 <= days <= 380:
            years.append((start, end, f))
    for start, end, f in years:
        inside = sorted((e for e, v in q.items() if v["start"] >= start and e < end))
        if end in q or len(inside) != 3:
            continue
        q[end] = {"value": f["val"] - sum(q[e]["value"] for e in inside),
                  "start": (date.fromisoformat(inside[-1]) + timedelta(days=1)).isoformat(),
                  "derived": True, "filed": f.get("filed")}
    return q


def _series(companyfacts: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    merged: dict[str, dict[str, Any]] = {}
    for concept in CONCEPTS[key]:
        for end, v in _quarters(_facts(companyfacts, concept, unit)).items():
            merged.setdefault(end, v)
    return merged


def _year_ago(series: dict[str, dict[str, Any]], end: str, slack: int = 10) -> float | None:
    target = date.fromisoformat(end) - timedelta(days=365)
    best = min(series, key=lambda e: abs((date.fromisoformat(e) - target).days), default=None)
    if best is None or abs((date.fromisoformat(best) - target).days) > slack:
        return None
    return series[best]["value"]


def _growth(now: float | None, before: float | None) -> float | None:
    return None if now is None or not before else round((now - before) / abs(before), 4)


def _margin(part: float | None, whole: float | None) -> float | None:
    return None if part is None or not whole else round(part / whole, 4)


def latest_quarter(companyfacts: dict[str, Any]) -> dict[str, Any] | None:
    """The newest reported quarter's growth and margins, as fractions (0.25 = 25%)."""
    s = {k: _series(companyfacts, k) for k in CONCEPTS}
    ends = sorted(set(s["revenue"]) | set(s["net_income"]))
    if not ends:
        return None
    end = ends[-1]
    val = {k: (s[k].get(end) or {}).get("value") for k in CONCEPTS}
    rev = val["revenue"]
    gross = val["gross_profit"]
    if gross is None and rev is not None and val["cost_of_revenue"] is not None:
        gross = rev - val["cost_of_revenue"]
    base = s["revenue"].get(end) or s["net_income"].get(end) or {}
    out = {
        "source": "sec_xbrl", "quarter_end": end, "filed": base.get("filed"),
        "derived_q4": bool(base.get("derived")),
        "revenue_usd": rev, "revenue_yoy": _growth(rev, _year_ago(s["revenue"], end)),
        "net_income_yoy": _growth(val["net_income"], _year_ago(s["net_income"], end)),
        "eps_diluted": round(val["eps_diluted"], 2) if val["eps_diluted"] is not None else None,
        "eps_yoy": _growth(val["eps_diluted"], _year_ago(s["eps_diluted"], end)),
        "gross_margin": _margin(gross, rev),
        "operating_margin": _margin(val["operating_income"], rev),
        "net_margin": _margin(val["net_income"], rev),
    }
    if val["net_income"] is not None and val["operating_income"] is not None and \
            val["net_income"] > val["operating_income"] > 0:
        out["note"] = "net income above operating income: gains outside the business are inside it"
    return out


class SecEdgar:
    """Cached SEC lookups. Every method returns None when SEC is unreachable or the ticker has
    no CIK (ETFs, most ADRs); callers treat that as "no data"."""

    def __init__(self, user_agent: str, source: HttpSource | None = None,
                 filings_ttl_s: float = 3600.0, facts_ttl_s: float = 24 * 3600.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.source = source or HttpSource("sec_edgar", timeout_s=20.0, user_agent=user_agent.strip())
        self.filings_ttl_s, self.facts_ttl_s = filings_ttl_s, facts_ttl_s
        self.clock, self.mono = clock, mono
        self._ciks: tuple[date, dict[str, str]] | None = None
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}

    def cik(self, symbol: str) -> str | None:
        today = self.clock().date()
        if self._ciks is None or self._ciks[0] != today:
            try:
                data = self.source.get_json(TICKERS_URL)
            except SourceUnavailable as e:
                log.info("SEC ticker list unavailable: %s", e)
                return (self._ciks[1] if self._ciks else {}).get(symbol.upper())
            ciks: dict[str, str] = {}
            for v in (data or {}).values():
                ciks.setdefault(str(v["ticker"]).upper().replace("-", "."), str(v["cik_str"]).zfill(10))
            self._ciks = (today, ciks)
        return self._ciks[1].get(symbol.upper())

    def _cached(self, kind: str, symbol: str, ttl: float, url: str, keep: Callable[[Any], Any]) -> Any:
        """keep(payload), cached for `ttl`: only the small result is kept, not the payload."""
        key = (kind, symbol.upper())
        hit = self._cache.get(key)
        if hit and self.mono() - hit[0] < ttl:
            return hit[1]
        cik = self.cik(symbol)
        if cik is None:
            return None
        try:
            data = self.source.get_json(url.format(cik=cik))
        except SourceUnavailable as e:
            log.info("SEC %s skipped for %s: %s", kind, symbol, e)
            return hit[1] if hit else None
        out = keep(data)
        self._cache[key] = (self.mono(), out)
        return out

    def filings(self, symbol: str, days: int = 7) -> dict[str, Any] | None:
        today = self.clock().date()
        recent = self._cached("submissions", symbol, self.filings_ttl_s, SUBMISSIONS_URL,
                              lambda sub: recent_8k(sub, today, 30))
        if recent is None:
            return None
        since = (today - timedelta(days=max(days, RED_FLAG_DAYS))).isoformat()
        return summarize_8k([f for f in recent if f["date"] >= since], today)

    def red_flags(self, symbol: str) -> list[str]:
        f = self.filings(symbol)
        return list(f["red_flags"]) if f else []

    def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        return self._cached("facts", symbol, self.facts_ttl_s, FACTS_URL, latest_quarter)
