# Live vs. backtest: what is the same, what is not, and how much it matters

## 1. What is the same (proved, see `verification/VERIFICATION_REPORT.md`)

Given the same one-minute bars, VIX and history, the bot makes **exactly** the original backtest's decisions: the same entries, the same options and
strikes, the same ranking and slot scheduling, the same exits on the same minutes, the same model P&L (largest difference anywhere: 0.0).
That holds on the 12-ticker ledger (110 trades), on a 24-ticker cross-check (131 trades), on 20 perturbed markets (2,814 trades), when the bot only
sees bars that have already closed, when polls arrive late and catch up, and after a restart mid-session. 20 of 20 deliberately broken bots were caught.

## 2. What is different live, and what I could measure

| Gap | Backtest | Live | Measured effect on the 21 recorded sessions |
|---|---|---|---|
| **VIX timing** | VIX at the same minute | Yahoo's `^VIX` runs about 15 minutes behind the equities | 0 trades lost, 0 entry minutes moved, **8 of 110 exits changed**; profit factor 1.86 -> 1.79, mean ROI +7.01% -> +6.60% (VIX delayed 15 min). The bot refuses to evaluate if VIX is more than 30 minutes stale. |
| **Volume / noise baselines** | first 14 sessions used interpolated 5-minute bars (no earlier 1-minute history existed); later sessions used 1-minute bars | always real 1-minute history (the last 14 full sessions) | Rebuilding every baseline from 5-minute bars (the only alternative I can build) changes **14 of 110 trades lost, 19 new**; profit factor 1.73, mean ROI +6.12%. Expect on the order of 10-15% of live signals to differ from what the published 110 would have been, in either direction. |
| **Missing minutes** | indexes by row, so one missing bar shifts all later minutes by one | indexes by the clock and forward-fills a hole | no change on the 21 sessions (one gap day, 8 tickers; 3 COIN days) |
| **Last bar still forming** | n/a | Yahoo returns the forming bar | the bot uses completed bars only, like the backtest. A ticker that lags a minute is held out of that minute's decisions and flagged. |
| **Alert latency** | enters at the signal bar's close, at the model price | the bot polls 6 seconds after each bar closes, then fetches; Yahoo's own delay adds to that (I have not measured it), and you fill after the alert | not measured. Entries more than 2.5 minutes late are marked LATE; more than 10 minutes, "not actionable". Every minute of delay moves the real price away from the model price, and 0DTE options move fast. |

## 3. What the simulator never contained (so no verification can address it)

- **Real option quotes.** Every price is Black-Scholes with a VIX-derived volatility and a flat half-spread of 0.5% or 0.8%. Real 0DTE spreads on single names are often much wider than that, and the real smile and bid/ask will differ from the model. Compare each alert with your live quote before acting; the alert says "MODEL" for this reason.
- **Whether a same-day expiry exists.** The backtest priced **every** ticker as 0DTE. SPY, QQQ and IWM list daily expiries; for most single names (NVDA, TSLA, META, AMD, AAPL, MSFT, AMZN, COIN, MSTR) a same-day expiry exists only on some weekdays, depending on current listings, which I did not look up. The bot does not check the real chain: on a day with no same-day expiry the alert still prints model strikes for a 0DTE that cannot be traded as written.
- **Fees, slippage and fills.** The simulator has no fee term. (The Master Report mentions $0.65 per contract; I could not find that applied anywhere in the simulation code, so treat reported returns as before costs.) A 1-minute-close stop on a 0DTE option can be gapped through.
- **Account rules.** Up to 7 trades a day can fire (4 + 2 + 1) with 3 open at once. At the default 25% per slot that is up to 75% of the account in 0DTE options at the same time, and a 0DTE option can lose its whole cost. If your account is subject to the pattern-day-trader rule, count day trades before relying on every alert; I have not checked the rule's current status for your broker.

## 4. How much evidence is the backtest?

110 trades over 21 sessions (2026-09-16 to 2026-10-14), one market regime, **thresholds chosen on the same data** (for example the RVOL, retrace and window filters).

| | Value |
|---|---|
| Trades / win rate / profit factor | 110 / 57.3% / 1.86 |
| Mean ROI per trade (on cost; credit spreads on collateral) | +7.0% (median +3.1%, standard deviation 31.8%) |
| 95% bootstrap interval, mean ROI per trade | +1.3% to +13.1% (trades resampled); +1.2% to +12.7% (days resampled, which respects clustering) |
| 95% bootstrap interval, profit factor | about 1.1 to 3.1 |
| Days with a positive sum of trade ROI | 16 of 21 |
| Share of gross profit from the 5 best trades | 27% (mean ROI without them: +3.1%) |
| By module (trades / mean ROI) | P1A 47 / +11.0% · P1B 7 / +14.9% · P1C 13 / +4.5% · **P2 37 / +1.5%** · P3 6 / +6.1% |

Those intervals are for sampling noise only. They do **not** include the effect of tuning the filters on this same data, model-versus-real option prices,
or fees, all of which push live results down. The honest reading is "a positive but small and uncertain edge on paper": Phase 2 (credit spreads) is close to
break-even, and Phase 1A (single 0DTE options) carries most of the result.

## 5. The right next step is a forward test

The bot writes its own forward record every evening (`ledger/trades.csv`, from final bars) and prints it against the backtest numbers in the EOD post.
Run it as alerts only, pre-register the number of trades and the pass/fail bar before looking (your other strategies use exactly this), and compare against your broker's real quotes
for the same signals. A kill-switch on the live record is **not** built in; if you add one, gate it on a full window the way the verdicts specify, not on the first few trades.
