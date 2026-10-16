"""Historical bar cache for research: data/bars_cache/<interval>/<SYMBOL>.csv.

`ctbot-fetch-bars` downloads regular-session bars from Robinhood (read-only tool
get_equity_historicals) in chunks that stay under the server's 5,000-bar cap, merges them
into the cache, and drops gap-fill (interpolated) bars. Robinhood's intraday history is
short (5-minute bars back to roughly late Feb 2026; 1-minute ~6 weeks), so re-running this
regularly grows the local history over time.
"""

from __future__ import annotations

import argparse
import csv
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ctbot.models import Bar

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/bars_cache")
# Trading days per call, kept well under the 5,000-bar cap.
DAYS_PER_CALL = {"minute": 10, "5minute": 50, "10minute": 100, "hour": 600, "day": 4500}
CAL_FACTOR = 1.45  # calendar days per trading day (weekends + holidays)


def cache_path(interval: str, symbol: str, root: Path = CACHE_DIR) -> Path:
    return root / interval / f"{symbol.upper()}.csv"


def load(interval: str, symbol: str, root: Path = CACHE_DIR) -> list[Bar]:
    path = cache_path(interval, symbol, root)
    if not path.exists():
        return []
    with path.open() as fh:
        return [
            Bar(symbol.upper(), datetime.fromisoformat(r["ts"]), float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r["volume"]))
            for r in csv.DictReader(fh)
        ]


def save(interval: str, symbol: str, bars: list[Bar], root: Path = CACHE_DIR) -> Path:
    path = cache_path(interval, symbol, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {b.ts: b for b in load(interval, symbol, root)}
    merged.update({b.ts: b for b in bars})
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in sorted(merged.values(), key=lambda b: b.ts):
            w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, int(b.volume)])
    return path


def chunks(since: date, until: date, interval: str) -> list[tuple[datetime, datetime]]:
    step = timedelta(days=int(DAYS_PER_CALL[interval] * CAL_FACTOR))
    out, start = [], datetime.combine(since, time(0), tzinfo=timezone.utc)
    end_all = datetime.combine(until + timedelta(days=1), time(0), tzinfo=timezone.utc)
    while start < end_all:
        end = min(start + step, end_all)
        out.append((start, end))
        start = end
    return out


def fetch(symbols: list[str], interval: str, since: date, until: date, call_many) -> dict[str, int]:
    """Download and cache bars. One symbol per call keeps responses small. Returns new-bar counts."""
    from ctbot.market_data.robinhood_readonly import parse_bars

    counts: dict[str, int] = {}
    for sym in symbols:
        calls = [
            ("get_equity_historicals", {
                "symbols": [sym.upper()], "interval": interval,
                "start_time": a.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_time": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            for a, b in chunks(since, until, interval)
        ]
        bars: list[Bar] = []
        for payload in call_many(calls):
            bars.extend(parse_bars(payload).get(sym.upper(), []))
        save(interval, sym, bars)
        counts[sym.upper()] = len(bars)
        log.info("%s %s: %d bars", sym.upper(), interval, len(bars))
    return counts


def main() -> None:
    from ctbot.config import Settings, load_universe
    from ctbot.market_data.robinhood_mcp_client import ReadOnlyRobinhoodClient

    p = argparse.ArgumentParser(description="Cache historical bars from Robinhood (read-only)")
    p.add_argument("--interval", default="5minute", choices=sorted(DAYS_PER_CALL))
    p.add_argument("--since", type=date.fromisoformat, default=date(2026, 2, 1))
    p.add_argument("--until", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    p.add_argument("symbols", nargs="*", help="default: config universe")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    symbols = args.symbols or load_universe(Settings.from_env().universe_file)
    counts = fetch(symbols, args.interval, args.since, args.until,
                   ReadOnlyRobinhoodClient(interactive=False).call_many)
    print(f"cached {sum(counts.values())} {args.interval} bars for {len(counts)} symbols in {CACHE_DIR}")


if __name__ == "__main__":
    main()
