# What the bot implements

Everything below is copied from the original backtest code (`verification/reference/`, pinned by SHA-256). Nothing is tuned or new.

**Universe:** SPY QQQ IWM NVDA TSLA META AMD AAPL MSFT AMZN COIN MSTR. **Data:** 1-minute bars; minute index `m` = minutes since 09:30 (0 to 389). Full sessions only.

## Five entry modules (first trigger of the day per ticker and module)

| Module | Signal window (m) | Trade | Entry filters | Stop / TP1 / max hold | Rank score |
|---|---|---|---|---|---|
| **P1A** Gap-Trap Sweep | 5-59 | single option, ~0.45 delta, against the gap | gap > 0.25%, closes through the 5-minute range against the gap, beyond VWAP and EMA9, still on the right side of yesterday's close; **m <= 35, retrace < 0.75, RVOL <= 1.25** | 26% / 90% / 50 min | 10 - RVOL |
| **P1B** Confirmed Gap + ORB15 | 18-64 | call debit spread (0.58 / 0.28 delta) | gap-up >= 0.35%, three closes above the 15-minute high, above the noise area, RVOL >= 1.25, z-score > 0.35, EMA9 >= EMA21, green bar; **enters at the next 5-minute boundary** | 25% / 50% / 60 min | 20 + RVOL |
| **P1C** ORB15 Bull-Trap Reject | 16-104 | put debit spread | poked above the 15-minute high, then a red bar back below it, under EMA9 and the prior low; **0.85 <= RVOL < 1.65 and close >= VWAP** | 28% / 45% / 65 min | 15 + RVOL |
| **P2** Midday Range-Wall Pin | 75-239 | credit spread (0.32 / 0.10 delta), against the rejection | rejection candle at the 75-minute high or low; **RVOL >= 1.0** | 20% / 16% / 80 min | 5 + RVOL |
| **P3** Afternoon Vanna Squeeze | 210-319 | call debit spread | flush below -1.3 SD from the open and -1.1 SD under VWAP, then a reclaim bar | 28% / 65% / 50 min | 12 + RVOL |

## Portfolio rules

- 3 capital slots. Per day at most 4 Phase-1 trades (P1A/B/C), 2 Phase-2, 1 Phase-3. One trade per ticker and phase per day.
- Candidates are ranked by entry minute, then higher score, then the original ticker and module order.
- A slot frees the minute **after** its trade exits: a new entry is never allowed in the exit minute.
- A signal in the last 15 bars of the session, or in minute 0, is not a trade.

## Exit ladder (evaluated once per completed bar, in this order)

1. If the bar's best price reaches TP1: sell half at the TP1 level; the stop becomes a floor at 60% of the TP1 gain.
2. Update the peak. After TP1, the floor trails at 72% of the peak gain.
3. After TP1: if the bar's worst price is at or below the floor, the rest exits; result = half at TP1 + half at the floor.
4. Before TP1: if the bar's **close** is at or below the stop, exit. The stop looks at the close, not the low.
5. At the hold limit (or 5 bars before the close), exit at the close.

## Pricing model (what "cost" and "ROI" mean)

Option prices are **Black-Scholes model prices, not quotes**: r = 4.5%; every trade is treated as a same-day (0DTE) expiry with 25 minutes of cushion;
implied vol = VIX/100 x a per-ticker multiplier with a skew adjustment, clipped to 10-220%; half-spreads of 0.5% (liquid names) or 0.8% plus a few cents;
strikes on a $1 or $2.50 grid. A credit spread's ROI is measured on its collateral (width - credit). The bot's alerts print these model numbers and say so.
