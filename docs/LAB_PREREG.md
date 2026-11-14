# Lab programme: pre-registration (frozen 2026-11-14, before any lab replay was run)

Scope: the observation-only research programme in `docs/LAB_PROGRAMME.md`. Nothing here changes a P2 signal, threshold,
exit, ticker, alert byte or the frozen P2 rule (`phase_review.RULE`). Every addition runs as a shadow variant, so the
100-trade P2 count that starts 2026-11-16 is unaffected.

## What was looked at before this freeze (so it cannot be claimed as blind)

* The P2 fair-vol backfill table (`ledger/p2_grading_backfill_2026-10-16_2026-11-13.csv`, 39 trades, 30 with a fair-vol
  grade) was read in aggregate (means by listed/unlisted).
* The three optimization studies' findings (all null) and the evidence-graded catalogue that motivated this programme.
* No lab feature (variance ratio, Noise Area, touch probability, event flags, correlation cap) had been computed on any
  P2 trade before this file was committed. The replay script refuses to run unless this file is committed and unmodified.

## Replay sample (descriptive only)

P2 trades dated 2026-10-16 to 2026-11-13 from the backfill (39 trades, 30 with fair-vol grade; 21 listed same-day).
Outcome: `roi_fair` (primary, n = 30), `roi_model` (secondary, n = 39). **With this many sessions, no replay result can be
confirmatory.** Replay results are reported to rank which shadow flags deserve the forward sample, nothing more. The
decisions come from the forward shadow ledger (P2 trades from 2026-11-16) and, when available, real-quote regrading.

## Pre-declared tests on the replay sample (Holm family of 8)

Statistic for tests R1-R7: mean(outcome | flag passes) minus mean(outcome | flag fails), per trade, one-sided
(passed better), session-clustered bootstrap, 10,000 draws, seed 20261114. Holm over the 8 tests. A flag also needs the
passed-subset one-sided 95% lower bound > 0 to be called "supported"; otherwise it is "not supported" or "underpowered"
(fewer than 8 trades on either side).

| ID | Flag (passes = trade would be KEPT) | Definition (causal at entry minute m) |
|---|---|---|
| R1 | Noise Area: inside band | price within [lower, upper] Noise Area (Zarattini-Aziz-Barbon), 14-session mean absolute move from open per minute, gap-adjusted |
| R2 | Noise Area: strike >= 1.0 x band | short strike distance beyond the band edge by at least 1.0 x band half-width |
| R3 | Noise Area: strike >= 1.5 x band | same with 1.5 |
| R4 | Variance ratio | VR(5) < 1 on the 1-minute log returns from the open to m (needs >= 30 bars) |
| R5 | Touch probability <= 0.25 | 2 x N(-ln(K1/S)/(sigma sqrt(T))) with sigma = fair-vol forecast, T = minutes to the close |
| R6 | Touch probability <= 0.35 | same threshold 0.35 |
| R7 | Side asymmetry | put-credit trades (direction +1) minus call-credit trades (direction -1), same bootstrap on the difference of means; "passes" = put-credit side |

R8 (forecast accuracy, not a trade test): the seasonality-adjusted remaining-session variance forecast vs the existing
14-session mean forecast, QLIKE loss on every (ticker, session) with >= 5 prior archived sessions, Diebold-Mariano
one-sided p, Newey-West lag 4. Support requires p < 0.05 after the Holm adjustment.

Descriptive only, no test: (a) FOMC day (2026-10-28) and CPI/NFP/PPI days in the window, (b) the one-index-slot cap
(static removal of P2 trades that overlap an open index-ETF trade): reports mean, worst session, CVaR95 of session
P&L before and after, (c) trades open after 15:30 (hard-exit check), (d) QLIKE of each realised-variance estimator.

## Forward programme (from 2026-11-16, shadow only)

Every P2 entry (alerted, withheld as unlisted, or muted) gets one row in `ledger/lab/p2_shadow.csv` computed at the
entry minute: all features, every flag below, the real-quote spread/credit/IV where a chain snapshot exists, and the
chain-derived GEX sign. Flags (all pre-declared, thresholds fixed here):

F1 VRP ratio = IV_mid(short strike) / forecast sigma, thresholds {1.0, 1.2, 1.4}.
F2 GEX sign (prior-close OI, assumed dealer convention long calls / short puts), one binary split.
F3 Scheduled-event mute: FOMC 12:00-16:00 ET; single-name earnings day-of and day-after; QQQ on NVDA/AAPL/MSFT/AMZN/META
   earnings reaction day; CPI/NFP/PPI/PCE/GDP days reported separately, expecting a null.
F4 Spread gate: (sum of leg half-spreads) / net credit <= {0.15, 0.25}; any leg quote older than 5 s fails.
F5 One-index-slot cap: another SPY/QQQ/IWM P2 slot already open fails.
F6 Noise Area inside-band; strike >= {1.0, 1.5} x band.
F7 VR(5) < 1.
F8 Touch probability <= {0.25, 0.35}.
F9 EV per collateral at fair sigma >= {0, +2%} (terminal-expiry proxy; real quotes).
F10 VIX1D / VIX top-tercile mute; VIX1D at 10:00 > 1.5 x its 20-day median mutes the day (judged on CVaR95 and worst
    session, not on the mean).
F11 XSP side-by-side: XSP net credit after half-spread >= SPY's.
F12 Side asymmetry: call-side vs put-side wall, real-quote mean difference.

Support criteria at the P2 verdict (100 trades) or at 150 shadow trades, whichever is later:
mean-improvement flags need passed-minus-failed one-sided clustered LB > 0 after Holm over all F-tests;
risk flags (F3, F5, F10) need a CVaR95-of-session-P&L improvement with LB > 0 and no significant mean loss;
measurement items (real-quote ledger, fill model, staleness) have no test; they are the evidence.

Pricing at the verdict: every result is reported at model price, fair-vol price, and real quote (mid and taker) when
quotes exist. Real-quote results outrank model and fair-vol results.

## Not tested, by design

Rejected by the evidence (see `docs/LAB_CATALOGUE.md`): max pain, pivots/Fibonacci, oscillators and trend overlays as
direction signals, ML meta-labelling and day clustering (fewer than 1,000 observations), calendars/diagonals, rolls,
delta hedging, social sentiment, unusual-activity products. Re-sweeping stops, targets, max-hold, delta and strike at
model prices is not repeated: the model IV error invalidates those paths.
