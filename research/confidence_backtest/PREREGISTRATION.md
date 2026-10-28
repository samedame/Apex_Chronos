# Pre-registration: does the confidence score predict outcomes, and should it be re-scored?

Frozen before any confirmatory test is run (the commit that adds this file precedes the analysis code's first run).
Nothing below is changed after results are seen; a deviation is reported as a deviation.

## Questions
1. **Q1.** Does the shipped confidence score (headline, and its setup / execution parts) predict wins or ROI on real bars?
2. **Q2.** Does any alert-time factor predict outcomes strongly and consistently enough to belong in a re-scored version?
3. **Q3.** How much of the strategy's edge survives realistic execution costs (wider spreads, late fills), and what does that say about the score's execution component?

## Data (all real 1-minute underlying bars; option prices are Black-Scholes model prices because no historical option quotes exist)
| Sample | Sessions | Source | Role |
|---|---|---|---|
| backtest | 21 (2026-09-16 .. 10-14) | `verification/fixtures.tar.gz` | derivation |
| forward | 10 (2026-10-15 .. 10-28) | Yahoo 1-minute bars snapshotted to `data/forward_1m.csv.gz`, replayed through the bot's own engine | held-out confirmation |

Unit of analysis is the trade; the **cluster is the session date** (trades on one day share a market). Every interval and p-value below uses a
**session-block bootstrap** (10,000 resamples of sessions with replacement). The forward replay reproduces 53 of the 55 ledger trades (the other
5 flips are data-vintage effects: Yahoo revises volumes after the close); the sensitivity run restricts forward to the 53 common trades.

## Already seen before this registration (reported, but exploratory - they cannot confirm anything)
* Per-module win rate and mean ROI in both samples (the shipped setup table).
* Spearman(shipped on-time score, ROI) = -0.10 backtest / +0.01 forward; the tier A / B table (tier A = index ETFs).
* Within-module Spearman of `rvol_entry`, `m_entry` and the bot's rank score vs ROI for P1A and P2: all |rho| <= 0.22, p >= 0.39, sign unstable.
* Single names out-earn index ETFs in model returns in both samples.

## Q1 - confirmatory tests of the shipped score
Score = `score_alert` with execution inputs the simulator cannot observe held at "on time, fresh, capital unset" (lateness cannot change a
model fill; see Q3 for the one place it can be measured).
* **T1** AUC of the headline score for `win` (roi > 0), per sample, with session-block bootstrap 95% CI.
* **T2** Spearman(score, ROI), same intervals. Repeated for the setup part and the execution part alone.
* **T3** Win rate and mean ROI by tier, with bootstrap CIs.
* **Decision R1:** the score "predicts" only if the AUC CI excludes 0.5 **on the same side in both samples**. Otherwise it is reported as not
  forecasting outcomes, whatever the point estimates look like.

## Q2 - candidate factors (fixed list of 11; no others will be tested confirmatorily)
`tclass_index` (SPY/QQQ/IWM = 1), `direction` (call = 1), `m_entry`, `rvol_entry`, `vix`, `abs_gap`, `gap_dir` (gap in the trade's direction),
`day_move_dir` (move since the open, in the trade's direction), `vwap_z_dir` (distance from VWAP in std units, in the trade's direction),
`rvol20` (the ticker's 20-day realised vol), `nth_of_day` (entry order that day; uses no outcomes).
* **Statistic:** within-module Spearman between factor and ROI (percentile ranks computed inside each module, then pooled), so a factor cannot
  win by merely standing in for the setup type.
* **Derivation (backtest):** Holm-adjusted bootstrap p < 0.10 across the 11 tests (liberal on purpose; the sample is small).
* **Confirmation (forward):** same sign and one-sided bootstrap p < 0.10.
* **Decision R2:** a factor enters a re-scored version only if it passes **both**. If none does, no data-fitted factor is added.
* The tests are repeated pooled (all 166 trades) as exploratory only.
* **Power:** the minimal detectable AUC (80% power, alpha 0.05, Hanley-McNeil variance inflated by the observed session design effect) is
  reported for each sample, so a null result is read as "this sample cannot see effects smaller than X", not "there is no effect".

## Q3 - execution stress on all 166 trades (strategy signals fixed; options re-priced)
* **S1 spread:** half-spread multiplied by 1, 1.5, 2, 3, 5 (full re-run of the 31 sessions, so slot scheduling reacts).
* **S2 delay:** fill delayed 0, 1, 2, 3, 5, 10 minutes with the strikes fixed at signal time, paying the then-current ask / receiving the bid;
  exits, stops and targets re-anchored to the delayed cost; the time stop stays anchored to the signal. Scheduling is held fixed (each trade
  independent). Reported with session-block bootstrap CIs on mean ROI, plus the OLS slope of mean ROI against delay minutes over 0-5.
* **Decision R3 (lateness factor):** the shipped factor gives full marks inside 150 s and falls to 0.3 at 600 s. If the measured loss exceeds
  **1.0 point of mean ROI per minute** (the pooled edge is about 4.6 points) the free window is too generous and the curve is re-based on the
  measurement; if the loss is below 0.3 points per minute the factor is over-weighted.
* **Decision R4 (level of the headline):** if mean ROI at 2x the spread is <= 0, a headline in tier A cannot be justified for any alert and the
  tier wording is changed to say the strategy's edge does not survive a realistic spread.

## Outputs
`results.json`, `REPORT.md`, and the project verdict. Re-running `python research/confidence_backtest/run_all.py` reproduces every number.
