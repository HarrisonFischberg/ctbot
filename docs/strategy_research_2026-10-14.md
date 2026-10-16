# Strategy research, 2026-10-14

All tests run on cached Robinhood data, long-only, with costs (2 bps per side for ETFs,
4 bps for stocks), split into in-sample (IS) and out-of-sample (OOS) periods.
5-minute data: 88 symbols, IS Feb 23 - Jun 30 2026, OOS Jul 1 - Oct 13 2026.
Daily data: IS 2010-2019, OOS 2020 - Oct 2026. Scripts were one-off research (scratchpad);
the adopted rules live in `src/ctbot/strategies/`.

## Per-trade results (after costs)

| Strategy | IS | OOS | Decision |
|---|---|---|---|
| ORB, 30-min range (ours) | -16.2 bps, n=592 | -2.4 bps, n=526 | keep on paper (near zero) |
| VWAP dip reversion | -8.1 bps, n=1,330 | -6.9 bps, n=1,127 | **disabled** |
| Overnight hold (close -> open) | +2.2 bps, n=3,781 | -4.3 bps, n=3,499 | keep on paper (near zero) |
| VWAP dip, volatility-scaled | negative | negative | rejected |
| Intraday momentum (first 30 min -> last 30 min) | -3 to -5 bps | -3 to -8 bps | rejected |
| ORB on "stocks in play" (Zarattini, Barbon & Aziz 2024), best variants | +2 to +7 bps | -3 to -11 bps | rejected (paper's edge needs a ~7,000-stock universe) |
| Noise-area intraday momentum SPY/QQQ (Zarattini, Aziz & Barbon 2024) | ~0 | ~0 | rejected (replications report the edge faded since 2025) |
| **RSI(2) on 14 ETFs** (Connors) | **+11.3 bps, 70% win, n=2,702** | **+16.8 bps, 69% win, n=1,872** | **adopted: `rsi2_etf`** |
| **Turn of month, SPY / QQQ** | **+19.0 / +33.0 bps** | **+34.3 / +47.1 bps** | **adopted: `turn_of_month`** |

## Portfolio level ($5,000, 25% per position, max 6)

| | IS 2010-2019 CAGR / max DD | OOS 2020-2026 CAGR / max DD |
|---|---|---|
| RSI(2) + turn of month | +5.0% / -19.6% | +3.5% / -21.2% |
| SPY buy & hold (price only) | +11.5% / -20.2% | +13.8% / -34.1% |
| QQQ buy & hold (price only) | +16.6% / -23.2% | +20.1% / -35.6% |
| QQQ only above its 200-day average | +9.7% / -18.7% | +18.7% / -21.9% (Sharpe 1.05 vs 0.86) |

## Conclusions

1. No intraday method tested beats costs on large caps; this matches studies of retail day
   traders (Barber, Lee, Liu & Odean, Taiwan: <1% persistently profitable; Chague et al., Brazil:
   ~97% of persistent day traders lost money).
2. RSI(2) and turn-of-month have real, persistent per-trade edges but, holding cash much of the
   time, trailed simply owning the index.
3. Highest return in this history came from staying invested in QQQ; a 200-day trend filter
   kept most of the return with ~40% less drawdown out-of-sample.

Sources: Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market"
(SSRN 4729284); Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)" (SSRN 4824172) and the codecat-ops replication; Columbia CEASA,
"Why Has PEAD Declined Over Time?"; Quantpedia, turn of the month; CXO Advisory on short-term
strategies and day-trader profitability.
