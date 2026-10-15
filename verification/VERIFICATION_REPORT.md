# APEX-CHRONOS bot: verification against the backtest

**Result: PASS** - the bot reproduces the original backtest trade-for-trade.
Recorded data: 21 sessions 2026-09-16 .. 2026-10-14 · numpy 2.2.6, pandas 2.3.3 · python 3.13.16 · 453s

| # | Check | Result | Detail |
|---|---|---|---|
| A | Oracle integrity (the original code, unmodified) | PASS | reference files match pinned SHA-256: yes; original script re-run on this data vs the saved apex_chronos_final_trades.csv: 110 vs 110 trades, every decision identical = True; 11 rows differ in modelled option cost (max $0.713/share) - the code is deterministic, so its inputs must have changed since the ledger was saved (cause not established; refreshed daily files are the likely one). The bot is compared with the fresh run, and the fresh run's statistics equal the published ones (see the table below) |
| B | Feature parity (VWAP, EMA9/21, noise area, RVOL, IV, prior close, 20d vol) | PASS | 252 ticker-sessions compared; worst absolute difference per feature: vwap 0.0e+00, vwap_std 0.0e+00, noise_upper 0.0e+00, rvol_cum 0.0e+00, ema9 0.0e+00, ema21 0.0e+00, iv 0.0e+00, prev_c 0.0e+00, rvol20 0.0e+00 |
| C | Candidate parity (every signal the original generated, before slot scheduling) | PASS | 198 oracle candidates vs 198 bot candidates; missing in bot 0, extra in bot 0; score diff 0.0e+00 |
| D | Trade-for-trade parity, 12-ticker portfolio (the 110-trade ledger) | PASS | 110 vs 110 trades; ticker/date/module/direction/entry minute/exit minute/exit reason/slot identical = True; max /diff/: cost_basis 0.0e+00, roi 0.0e+00, peak_roi 0.0e+00, rvol_entry 0.0e+00, score 0.0e+00 |
| E | Extended universe: 24 tickers (more candidates, scheduler under pressure) | PASS | candidates 364 vs 364 (identical sets: True); trades 131 vs 131 (decisions identical: True); max /diff/ cost_basis 0.0e+00, roi 0.0e+00, peak_roi 0.0e+00, rvol_entry 0.0e+00, score 0.0e+00 |
| F | Live-path replay: each minute sees ONLY bars up to that minute (no look-ahead) | PASS | 110 trades identical to oracle = True; candidate sets identical = True; full event stream (every ENTRY/TP1/EXIT with its minute) identical to the all-day-data run = True; 24-ticker live-path: 131 trades, identical to oracle = True; 74s |
| G | Poll-cadence invariance (a poll that arrives late and catches up several minutes at once) | PASS | burst sizes 3 min: identical, 7 min: identical, 15 min: identical, 45 min: identical |
| H | Restart equivalence (cold start + catch-up == ran all day) | PASS | 252 cold-start comparisons at 12 cut points x 21 sessions; mismatches: 0 |
| I | Scheduler invariants (3 slots, phase limits 4/2/1, one trade per ticker-phase, no slot overlap) | PASS | bot 12-ticker violations 0, 24-ticker violations 0 (oracle itself: 0) |
| J | Differential fuzzing: original vs bot on perturbed markets (prices, gaps, volume, VIX) | PASS | 20 different markets x 24 tickers x 21 sessions: 8158 candidates and 2814 scheduled trades compared, seeds with ANY difference: none; live-path (no look-ahead) re-check on 2 of them: identical; exits {'time_stop': 1395, 'stop_loss': 1150, 'profit_trail': 269}; per module {'Phase1A_GapTrap_Single0DTE': 1262, 'Phase1B_ConfirmedGap_DebitSpread': 139, 'Phase1C_BullTrap_PutDebitSpread': 212, 'Phase2_MiddayPin_CreditSpread': 834, 'Phase3_AfternoonVanna_CallSpread': 367}; 261s |
| K | Mutation power: deliberately broken bots must be detected | PASS | 20/20 injected faults detected; 1 provably-equivalent fault(s) excluded (smallest TP1 overshoot seen in 431 touches: 0.0014 ROI points above the target, never exactly on it) |
| L | Live-vs-backtest gaps measured on the same data (informational, not pass/fail) | info | Live loader's clock-grid indexing (holes forward-filled) instead of the original's row indexing: 0 of 110 trades lost, 0 new, 0 entry minutes moved, 0 exits changed, mean /ROI change/ 0.00 pts; VIX delayed 5 min (Yahoo's ^VIX runs ~15 min behind the equities): 0 of 110 trades lost, 0 new, 0 entry minutes moved, 7 exits changed, mean /ROI change/ 0.49 pts; VIX delayed 15 min (Yahoo's ^VIX runs ~15 min behind the equities): 0 of 110 trades lost, 0 new, 0 entry minutes moved, 8 exits changed, mean /ROI change/ 1.02 pts; Volume/noise baselines rebuilt from 5-minute bars for every session (the original used 5-minute data only for its first 14 sessions; live always has real 1-minute history): 14 of 110 trades lost, 19 new, 3 entry minutes moved, 3 exits changed, mean /ROI change/ 0.32 pts |

## Mutation results (does the verification have teeth?)

| Injected fault | Verdict | How / why |
|---|---|---|
| P1A RVOL cap 1.25 -> 1.40 | detected (recorded data, 12 tickers) | 3 candidate differences; 15 trade differences |
| P1A retrace cap 0.75 -> 0.90 | detected (recorded data, 12 tickers) | 3 candidate differences; 10 trade differences |
| P1C RVOL ceiling 1.65 -> 1.40 | detected (recorded data, 12 tickers) | 6 candidate differences; 10 trade differences |
| P2 RVOL floor 1.00 -> 0.80 | detected (recorded data, 12 tickers) | 17 candidate differences; 20 trade differences |
| Phase-1 daily limit 4 -> 3 | detected (recorded data, 12 tickers) | 14 trade differences |
| 4 capital slots instead of 3 | detected (recorded data, 12 tickers) | 20 trade differences |
| slot reusable in the minute its trade exits (exits processed before entries) | detected (recorded data, 12 tickers) | 6 trade differences |
| trail 72% -> 60% of peak | detected (recorded data, 12 tickers) | 22 trade differences |
| post-TP1 floor 60% -> 50% of the target | equivalent - cannot change an outcome | after a TP1 touch the peak is >= the target, so the 72%-of-peak trail is always above the 60%-of-target floor; the floor can only bind if a bar touches the target to the last bit |
| risk-free rate 4.5% -> 3% | detected (recorded data, 12 tickers) | numeric differences |
| half-spread (liquid names) 0.5% -> 1.0% | detected (recorded data, 12 tickers) | 10 trade differences |
| P2 stop-loss 20% -> 25% | detected (recorded data, 12 tickers) | 6 trade differences |
| P3 take-profit 65% -> 60% | detected (recorded data, 24 tickers) | 2 trade differences |
| P1A max hold 50 -> 45 min | detected (recorded data, 12 tickers) | 58 trade differences |
| P1B score base 20 -> 10 (changes ranking) | detected (recorded data, 12 tickers) | 0 candidate differences; numeric differences |
| E1 entry window ends at minute 20 instead of 60 | detected (recorded data, 12 tickers) | 8 candidate differences; 18 trade differences |
| U5 window starts at minute 80 instead of 75 | detected (recorded data, 12 tickers) | 21 candidate differences; 15 trade differences |
| E1 peeks one bar ahead (look-ahead) | detected (recorded data, 12 tickers) | 92 candidate differences; 97 trade differences |
| EMA9 computed with span 10 | detected (recorded data, 12 tickers) | 19 candidate differences; 12 trade differences |
| implied vol one bar stale | detected (recorded data, 12 tickers) | 10 trade differences |
| RVOL baseline over 7 sessions instead of 14 | detected (recorded data, 12 tickers) | 51 candidate differences; 70 trade differences |

## Live-vs-backtest gaps (same data, bot engine)

| Scenario | Trades | Win rate | Profit factor | Mean ROI/trade | Difference from the backtest |
|---|---|---|---|---|---|
| Backtest as published (oracle) | 110 | 57.3% | 1.86 | +7.01% | - |
| Live loader's clock-grid indexing (holes forward-filled) instead of the original's row indexing | 110 | 57.3% | 1.86 | +7.01% | 0 trades lost, 0 new, 0 entry minutes moved, 0 exits changed |
| VIX delayed 5 min (Yahoo's ^VIX runs ~15 min behind the equities) | 110 | 58.2% | 1.87 | +7.07% | 0 trades lost, 0 new, 0 entry minutes moved, 7 exits changed |
| VIX delayed 15 min (Yahoo's ^VIX runs ~15 min behind the equities) | 110 | 58.2% | 1.79 | +6.60% | 0 trades lost, 0 new, 0 entry minutes moved, 8 exits changed |
| Volume/noise baselines rebuilt from 5-minute bars for every session (the original used 5-minute data only for its first 14 sessions; live always has real 1-minute history) | 115 | 56.5% | 1.73 | +6.12% | 14 trades lost, 19 new, 3 entry minutes moved, 3 exits changed |

## What this proves, and what it does not

**Proven (against the original code, bit for bit):** given the same one-minute bars, VIX and history, the bot makes the same entries, picks the same options and strikes, ranks and schedules them the same way, and takes the same exits at the same minutes with the same model P&L. It does so using only bars that have already closed, and it gives the same answer whether it is polled every minute, polls late and catches up, or restarts mid-session.

**Not proven by this report:**
- that the backtest result itself is a durable edge. It is 21 sessions, one regime, with thresholds fitted on the same data;
- that Black-Scholes model prices resemble the real bid/ask you will be quoted. The backtest never used real option quotes;
- that a same-day expiry exists for the strike and ticker in an alert (the backtest assumed 0DTE for every name);
- the live data path end to end (Yahoo availability, bar timing, GitHub scheduling). The EOD reconcile job measures that every day instead;
- anything about fills, slippage, fees or pattern-day-trader limits, none of which are in the simulator.

