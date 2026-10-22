"""Sector of each symbol in config/universe.txt, for the sector cap (RiskLimits.max_sector_pct).

Index funds count toward the sector they are mostly made of: the Nasdaq-100 (QQQ, and QLD at
twice its value since it is 2x leveraged) is Technology, so a book holding QQQ plus chip stocks
sees one big Technology bet. Broad funds (SPY, IWM, DIA) are "Broad market". Unknown symbols
are their own sector, so they never share a cap with anything.
"""

from __future__ import annotations

SECTORS: dict[str, str] = {
    # funds
    "SPY": "Broad market", "IWM": "Broad market", "DIA": "Broad market",
    "QQQ": "Technology", "QLD": "Technology", "TQQQ": "Technology", "XLK": "Technology", "SMH": "Technology",
    "XLF": "Financials", "XLE": "Energy", "XLV": "Health care", "XLY": "Consumer", "XLP": "Staples",
    "XLI": "Industrials", "XLU": "Utilities", "XLB": "Materials", "XLC": "Communication",
    "GLD": "Gold", "TLT": "Bonds",
    # technology (chips, software, hardware)
    **{s: "Technology" for s in (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE", "CSCO", "INTC", "QCOM", "TXN",
        "INTU", "AMAT", "MU", "LRCX", "PLTR", "MRVL", "PANW", "CRWD", "ANET", "NOW", "SHOP")},
    **{s: "Communication" for s in ("GOOGL", "META", "NFLX", "DIS")},
    **{s: "Consumer" for s in ("AMZN", "TSLA", "HD", "LOW", "NKE", "MCD", "SBUX", "UBER")},
    **{s: "Staples" for s in ("PG", "COST", "PEP", "KO", "WMT")},
    **{s: "Financials" for s in (
        "BRK.B", "JPM", "V", "MA", "BAC", "GS", "MS", "C", "WFC", "SCHW", "BLK", "PYPL", "COIN", "HOOD")},
    **{s: "Health care" for s in ("UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "ISRG")},
    **{s: "Energy" for s in ("XOM", "CVX")},
    **{s: "Industrials" for s in ("CAT", "DE", "GE", "BA", "HON", "UPS")},
}

# Leveraged funds count at their leverage: $1,000 of QLD moves like $2,000 of QQQ.
LEVERAGE: dict[str, float] = {"QLD": 2.0, "TQQQ": 3.0, "SSO": 2.0, "UPRO": 3.0}


def sector(symbol: str) -> str:
    return SECTORS.get(symbol.upper(), symbol.upper())


def exposure(positions: dict[str, float]) -> dict[str, float]:
    """symbol -> market value  =>  sector -> dollars of exposure (leverage included)."""
    out: dict[str, float] = {}
    for sym, value in positions.items():
        k = sector(sym)
        out[k] = out.get(k, 0.0) + value * LEVERAGE.get(sym.upper(), 1.0)
    return out
