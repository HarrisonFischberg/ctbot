"""Pull back after losses: when the bot stops borrowing.

The account is "pulled back" while any trigger holds:
* the day is down at least risk_off_day_loss_pct (clears the next trading day);
* equity is at least risk_off_drawdown_pct below its best (clears as it recovers);
* the trend symbol (QQQ) trades under its risk_off_trend_days average of completed daily closes
  (clears when it climbs back above).

While pulled back, buys may spend only cash on hand: paper margin acts as 1x, so a book that is
already borrowing makes no new buys (reviewed entries and trend-core top-ups alike) until sales
or gains pay the loan down. Nothing is sold to deleverage; stops, trails and exits run as before.
Stateless: every cycle re-checks the triggers, so full risk comes back on once none holds.
"""

from __future__ import annotations

from datetime import date

from ctbot.analysis import stats


def trend_level(md, symbol: str, days: int) -> float | None:
    """The `days`-day average of `symbol`'s completed daily closes (None if unavailable)."""
    if days <= 0:
        return None
    try:
        closes = [b.close for b in md.daily_bars(symbol, days + 5)]
    except Exception:  # noqa: BLE001 - a missing trend is "no trigger", never a blocker
        return None
    return float(stats.sma(closes, days)) if len(closes) >= days else None


def reasons(limits, equity: float, day_start_equity: float, peak_equity: float,
            trend_price: float | None, trend_avg: float | None) -> list[str]:
    """Plain-language reasons the account is pulled back now (empty = full risk)."""
    out = []
    if limits.risk_off_day_loss_pct > 0 and day_start_equity > 0:
        day = equity / day_start_equity - 1
        if day <= -limits.risk_off_day_loss_pct:
            out.append(f"down {-day:.1%} today")
    if limits.risk_off_drawdown_pct > 0 and peak_equity > 0:
        dd = equity / peak_equity - 1
        if dd <= -limits.risk_off_drawdown_pct:
            out.append(f"{-dd:.1%} below its best")
    if trend_price and trend_avg and trend_price < trend_avg:
        out.append(f"{limits.risk_off_trend_symbol} under its {limits.risk_off_trend_days}-day average")
    return out


class TrendCache:
    """One trend average per trading day (daily bars don't change during the session)."""

    def __init__(self) -> None:
        self.day: date | None = None
        self.avg: float | None = None

    def get(self, md, symbol: str, days: int, today: date) -> float | None:
        if self.day != today or self.avg is None:  # a failed fetch (e.g. a data outage) retries
            self.day, self.avg = today, trend_level(md, symbol, days)
        return self.avg
