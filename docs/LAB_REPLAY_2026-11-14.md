# Lab replay on the archived P2 trades (2026-10-16 to 2026-11-13)

Pre-registration verified before running: `docs/LAB_PREREG.md` sha256 `d2ad432655ec9202...`, frozen in commit `519ba862`.

Sample: 39 P2 trades, 30 with a fair-vol grade. **Descriptive only.** With roughly 20 sessions no row below is confirmatory; the table ranks which shadow flags earn a place in the forward sample. Outcome: `roi_fair` (primary, in the Holm family), `roi_model` (secondary, reported raw).

## R1-R7 (passed minus failed, per trade, one-sided, session-clustered bootstrap)

| ID | Flag | n pass / fail | mean pass | mean fail | diff | raw p | Holm p (of 8) | LB95 of passed mean | verdict | secondary (model prices): n, diff, raw p |
|---|---|---|---|---|---|---|---|---|---|---|
| R1 | Noise Area: inside band | 21 / 9 | +0.11% | +2.47% | -2.35% | 0.719 | 1.000 | -5.17% | **not supported** | 21+9, -2.33%, p 0.721 |
| R2 | Noise Area: strike >= 1.0 x band | 7 / 23 | +1.12% | +0.73% | +0.39% | 0.467 | 1.000 | -3.61% | **underpowered** | 7+23, +2.05%, p 0.344 |
| R3 | Noise Area: strike >= 1.5 x band | 7 / 23 | +1.12% | +0.73% | +0.39% | 0.467 | 1.000 | -3.61% | **underpowered** | 7+23, +2.05%, p 0.344 |
| R4 | VR(5) < 1 | 19 / 11 | +0.67% | +1.09% | -0.42% | 0.534 | 1.000 | -5.17% | **not supported** | 25+14, -5.31%, p 0.986 |
| R5 | Touch probability <= 0.25 | 6 / 24 | +4.07% | +0.01% | +4.06% | 0.117 | 0.819 | +1.40% | **underpowered** | 6+24, -0.51%, p 0.579 |
| R6 | Touch probability <= 0.35 | 15 / 15 | -2.00% | +3.64% | -5.63% | 0.930 | 1.000 | -7.92% | **not supported** | 15+15, +0.21%, p 0.458 |
| R7 | Put-credit minus call-credit | 12 / 18 | -0.45% | +1.67% | -2.12% | 0.645 | 1.000 | -9.19% | **not supported** | 16+23, +1.37%, p 0.349 |

## R8: remaining-session variance forecast (QLIKE, lower is better)

Evaluated at minute 120 (11:30 ET) for every (ticker, session) with at least 5 prior archived sessions: 16 sessions, 192 ticker-session pairs. **The pre-registration does not name the minute; this is an implementation choice, stated here.** The DM test runs on the per-session mean loss differential (Newey-West lag 4); the pooled pair-level result is a sensitivity check only.

| | seasonality forecast | 14-session mean |
|---|---|---|
| mean QLIKE (session means) | 0.494 | 0.703 |

DM (sessions): stat -1.77, one-sided p 0.039, Holm p 0.310 -> **not supported**. Pooled sensitivity: p 0.026 (n 192).

QLIKE of single estimators as rest-of-day forecasts (morning estimate scaled by bar count, no seasonality; descriptive):

| estimator | mean QLIKE |
|---|---|
| close_to_close | 0.705 |
| parkinson | 0.743 |
| garman_klass | 0.751 |
| rogers_satchell | 0.777 |
| seasonality_forecast | 0.494 |
| mean14 | 0.703 |

## Descriptive items

**(a) Event days in the window** (calendar covers the Oct 28 FOMC and the Nov 6/10/13 releases; the October CPI/PPI/NFP dates fall before 10-16 or are not in the calendar):

| date | events | P2 trades | sum roi_model | mean roi_fair |
|---|---|---|---|---|
| 2026-10-28 | fomc_day | 2 | +12.60% | +6.68% |
| 2026-11-06 | nfp_day | 2 | -4.60% | -18.32% |
| 2026-11-10 | cpi_day | 2 | +10.10% | +6.37% |
| 2026-11-13 | ppi_day | 1 | +2.17% | -3.23% |

**(b) One-index-slot cap** (static removal of a P2 trade that overlaps an open SPY/QQQ/IWM P2 trade; session P&L = 10% x sum of trade ROI):

| outcome | | sessions | mean | worst session | CVaR95 | trades removed |
|---|---|---|---|---|---|---|
| roi_model | before | 21 | +0.792% | -2.378% | -2.064% |  |
| roi_model | after | 21 | +0.683% | -2.378% | -2.064% | 2 |
| roi_fair | before | 16 | +0.154% | -4.384% | -4.384% |  |
| roi_fair | after | 16 | +0.154% | -4.384% | -4.384% | 0 |

**(c) Trades still open after 15:30 ET:** 0 of 39 at model exits; 0 at fair-vol exits.

Coverage of the feature columns (trades with a finite value): {'f6_inside_band': 30, 'vr5_lt1': 39, 'f8_touch_le_25': 30, 'strike_beyond_band': 30}.

Rows are in `ledger/lab/p2_shadow_backfill.csv`. Nothing here changes P2.
