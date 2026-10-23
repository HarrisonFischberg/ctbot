"""House members' stock trades from the House Clerk's public PTRs (free, no key).

The day-trader pack's trader-pelosi (github.com/Jakeschincariol/day-trader-skill, MIT) as a
CONGRESS_TRADE provider for the congressional strategy, which until now needed a paid feed
(Quiver, Unusual Whales). Members must report trades over $1,000 within 45 days (the STOCK
Act), so these are public filings read quickly, not early information.

How it works, at most once every `refresh_hours`:
1. Download the year's filing index (`<YEAR>FD.zip`, one <Member> per filing) and keep the
   Periodic Transaction Reports (type P) filed in the last `lookback_days`.
2. Download each new PTR PDF once (`max_new_per_refresh` per refresh, politely) and turn it
   into rows with `pdftotext -layout` (poppler: `brew install poppler`). Without pdftotext no
   PDF can be read: the provider logs that once and returns what it already parsed.
3. Each stock row with a ticker becomes one event: purchase -> BUY, sale -> SELL, actor = the
   member, event date = the trade date, disclosed = the filing date, size = the low end of the
   amount range ("$15,001 - $50,000" -> 15001). Options, shares from exercising an option
   bought long ago, exchanges and rows with no ticker are skipped: none is a fresh share trade.

Parsed reports are kept in `cache_dir`/ptrs.json, so a restart re-downloads nothing. Senate
reports sit behind a terms agreement a person has to accept; they are not automated.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ElementTree
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ctbot.models import AltDataEvent, EventKind, Side
from ctbot.net import HttpSource, SourceUnavailable

log = logging.getLogger(__name__)

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child", "": "self"}


def _iso(mdy: str | None) -> str | None:
    """'8/21/2026' -> '2026-08-21' (two-digit years -> 20xx)."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year + 2000 if year < 100 else year, month, day).isoformat()
    except ValueError:
        return None


def parse_index(data: bytes, year: int) -> list[dict[str, Any]]:
    """Every filing in a <YEAR>FD.zip (or its XML), newest first."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                return []
            data = z.read(names[0])
    out = []
    for m in ElementTree.fromstring(data).iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        name = " ".join(x for x in (f.get("First"), f.get("Last"), f.get("Suffix")) if x)
        out.append({"doc_id": f.get("DocID", ""), "type": f.get("FilingType", ""), "name": name,
                    "district": f.get("StateDst", ""), "year": int(f.get("Year") or year),
                    "filed": _iso(f.get("FilingDate", ""))})
    out.sort(key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)
    return out


# ---- parsing a PTR (pdftotext -layout output); ported from trader-pelosi/ptr.py -------------

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABELS = [
    ("status", re.compile(r"^\s*F(?:ILING)?\s+S(?:TATUS)?\s*:\s*(.*)$", re.I)),
    ("description", re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)),
    ("account", re.compile(r"^\s*S(?:UBHOLDING)?\s+O(?:F)?\s*:\s*(.*)$", re.I)),
    ("location", re.compile(r"^\s*L(?:OCATION)?\s*:\s*(.*)$", re.I)),
]
# Column headers repeat at the top of every page, sometimes in the middle of a row.
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)", re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)
AMOUNT = re.compile(r"\$([\d,]+)")
OPTION = re.compile(r"\b(call|put)\s+options?\b|\boptions?\b.*\bstrike\b", re.I)


def _finish(row: dict[str, Any]) -> dict[str, Any]:
    asset = " ".join(row.pop("_asset").split())
    tick = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
    code = re.findall(r"\[([A-Z]{2})\]", asset)
    row["ticker"] = tick[-1] if tick else None
    row["asset_type"] = code[-1] if code else None
    row["asset"] = re.sub(r"\s*\[[A-Z]{2}\]", "", asset).strip(" -")
    row["amount"] = " ".join(row["amount"].split())
    nums = [int(x.replace(",", "")) for x in AMOUNT.findall(row["amount"])]
    row["amount_low"] = nums[0] if nums else None
    row["amount_high"] = nums[1] if len(nums) > 1 else None
    row["description"] = " ".join(row.get("description", "").split()) or None
    row["option"] = bool(row["asset_type"] == "OP" or OPTION.search(row["description"] or ""))
    return row


def parse_ptr_text(text: str) -> list[dict[str, Any]]:
    """Transaction rows: owner, asset, ticker, asset_type, type, date, amount, amount_low/high,
    description, option."""
    rows: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    mode, amount_open = None, False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        t = TXN.match(line)
        if t and _iso(t.group("date")):
            if cur:
                rows.append(cur)
            owner = t.group("owner") or ""
            cur = {"owner": OWNERS.get(owner, owner), "_asset": t.group("asset"), "type": t.group("type"),
                   "type_name": TXN_TYPES.get(t.group("type"), t.group("type")),
                   "date": _iso(t.group("date")), "amount": t.group("amount")}
            amount_open, mode = cur["amount"].endswith("-"), "asset"
            continue
        if cur is None:
            continue
        if not s:
            if mode in ("description", "asset"):
                mode = "labels" if mode == "description" or "[" in cur["_asset"] else mode
            continue
        if PAGE_HEADER.match(s):
            continue
        if END.match(s):
            mode = "labels"
            continue
        label = next(((k, rx.match(line)) for k, rx in LABELS if rx.match(line)), None)
        if label:
            key, lm = label
            if key == "description":
                cur["description"], mode = lm.group(1).strip(), "description"
            else:
                mode = "labels"
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["_asset"] += " " + " ".join(parts)
        elif mode == "description":
            cur["description"] = cur.get("description", "") + " " + s
    if cur:
        rows.append(cur)
    return [_finish(r) for r in rows]


def pdf_to_text(pdf: bytes) -> str | None:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            fh.write(pdf)
            fh.flush()
            out = subprocess.run([exe, "-layout", fh.name, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def to_events(report: dict[str, Any]) -> list[AltDataEvent]:
    out = []
    filed = date.fromisoformat(report["filed"])
    for r in report.get("rows", []):
        side = {"P": Side.BUY, "S": Side.SELL, "S (partial)": Side.SELL}.get(r.get("type"))
        tick = r.get("ticker")
        if side is None or not tick or r.get("option") or r.get("asset_type") not in (None, "ST"):
            continue
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", tick) or not r.get("date"):
            continue
        out.append(AltDataEvent(
            EventKind.CONGRESS_TRADE, tick, side, report["name"], date.fromisoformat(r["date"]), filed,
            "house_clerk", float(r["amount_low"]) if r.get("amount_low") else None,
            {"doc_id": report["doc_id"], "owner": r.get("owner"), "amount": r.get("amount"),
             "district": report.get("district")}))
    return out


class HouseClerkProvider:
    name = "house_clerk"
    supports = frozenset({EventKind.CONGRESS_TRADE})

    def __init__(self, cache_dir: Path, source: HttpSource | None = None, lookback_days: int = 45,
                 refresh_hours: float = 6.0, max_new_per_refresh: int = 40, pause_s: float = 0.5,
                 to_text: Callable[[bytes], str | None] = pdf_to_text,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cache_dir = cache_dir
        self.source = source or HttpSource("house_clerk", timeout_s=30.0)
        self.lookback_days, self.refresh_hours = lookback_days, refresh_hours
        self.max_new_per_refresh, self.pause_s = max_new_per_refresh, pause_s
        self.to_text, self.clock, self.sleep = to_text, clock, sleep
        self._checked: datetime | None = None
        self._warned = False

    @property
    def _path(self) -> Path:
        return self.cache_dir / "ptrs.json"

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, reports: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reports, indent=1))
        tmp.replace(self._path)

    def refresh(self) -> dict[str, Any]:
        reports = self._load()
        now = self.clock()
        if self._checked and now - self._checked < timedelta(hours=self.refresh_hours):
            return reports
        self._checked = now
        today = now.date()
        since = (today - timedelta(days=self.lookback_days)).isoformat()
        filings = []
        for year in sorted({today.year, (today - timedelta(days=self.lookback_days)).year}):
            try:
                filings += parse_index(self.source.get_bytes(INDEX_URL.format(year=year)), year)
            except (SourceUnavailable, ElementTree.ParseError, zipfile.BadZipFile) as e:
                log.info("House Clerk index %d unavailable: %s", year, e)
        new = [f for f in filings if f["type"] == "P" and (f["filed"] or "") >= since
               and f["doc_id"] and f["doc_id"] not in reports]
        for f in new[: self.max_new_per_refresh]:
            try:
                pdf = self.source.get_bytes(PTR_URL.format(year=f["year"], doc=f["doc_id"]))
            except SourceUnavailable as e:
                log.info("House PTR %s unavailable: %s", f["doc_id"], e)
                break
            text = self.to_text(pdf)
            if text is None:
                if not self._warned:
                    log.warning("house_clerk: pdftotext not found (brew install poppler); PTRs can't be read")
                    self._warned = True
                break
            reports[f["doc_id"]] = {**f, "rows": parse_ptr_text(text)}
            self.sleep(self.pause_s)
        reports = {k: v for k, v in reports.items() if (v.get("filed") or "") >= since}
        self._save(reports)
        return reports

    def fetch(self, kinds: set[EventKind], since: date) -> list[AltDataEvent]:
        if EventKind.CONGRESS_TRADE not in kinds:
            return []
        out = []
        for rep in self.refresh().values():
            if rep.get("filed") and date.fromisoformat(rep["filed"]) >= since:
                out.extend(to_events(rep))
        return out
