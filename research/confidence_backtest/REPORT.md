# Does the confidence score predict wins? Backtest on real bars, 2026-10-28

Pre-registered in `PREREGISTRATION.md` (committed before any confirmatory test ran). Real 1-minute underlying bars, 31 sessions, 166 trades
(110 backtest + 56 forward). Option prices are Black-Scholes model prices because no historical option quotes exist.
Reproduce: `python research/confidence_backtest/run_all.py`.

## Data
| Sample | Sessions | Trades | Win | Mean ROI |
|---|---|---|---|---|
| backtest (fixtures) | 21 | 110 | 57.3% | +7.01% |
| forward (Yahoo bars re-pulled and replayed through the bot's own engine) | 10 | 56 | 44.6% | -0.17% |
| pooled | 31 | 166 | 53.0% | +4.59% (95% CI -0.6 to +9.5) |

The forward replay reproduces 53 of the 55 ledger trades (ROI differences < 0.01 point). The other 5 flips (10/20, 10/27) are a data-vintage effect:
the replay of today's bars reproduces the entries the *live* bot alerted that the end-of-day "final" replay later dropped. Restricting forward to
the 53 common trades changes nothing (AUC 0.525, 95% CI 0.39-0.65).

## Q1 - does the shipped score predict outcomes? **No (R1).**
AUC of the headline score for a win, session-block bootstrap 95% CI:

| Sample | AUC | 95% CI | Spearman vs ROI |
|---|---|---|---|
| backtest | 0.480 | 0.37-0.58 | -0.09 |
| forward | 0.514 | 0.39-0.63 | +0.04 |
| pooled | 0.493 | 0.41-0.57 | -0.03 |

Both intervals straddle 0.5, so R1 fails. An independent re-implementation reproduces these. The forward trades scored with the lateness the live bot
actually posted (6-25 s) give AUC 0.494 (44 trades): lateness in that range does not vary enough to matter.

**What the "levels" are.** On time and fresh, the score takes only four values (74, 75, 91, 92) and the tier split is simply index ETFs versus everything else:

| Tier | Trades | Win (95% CI) | Mean ROI (95% CI) |
|---|---|---|---|
| A, 85-100 (SPY/QQQ/IWM) | 52 | 46% (35-57) | -1.2% (-6.6 to +4.7) |
| B, 70-84 (everything else) | 114 | 56% (47-66) | +7.2% (+0.6 to +13.7) |

Tier A, the "most actionable", has had the *worst* model results in both samples (backtest +1.3%, forward -6.7%). Its score is high because a same-day
expiry is known to exist for those tickers, not because the trades work. Tier B's returns rest on an assumed 0DTE expiry that may not be tradeable.
The setup part alone has AUC 0.58 (0.50-0.67) pooled and the execution part 0.46 (0.39-0.52), but the setup number is partly in-sample (see D1).

## Q2 - does any alert-time factor predict outcomes? **None of 11 qualifies (R2).**
Within-module Spearman against ROI. Backtest derivation, Holm-adjusted p; forward confirmation.

| Factor | Backtest rho (95% CI) | Holm p | Forward rho (95% CI) | Same sign |
|---|---|---|---|---|
| tclass_index | -0.07 (-0.27, +0.10) | 1.00 | +0.07 (-0.18, +0.28) | no |
| direction | -0.05 (-0.31, +0.19) | 1.00 | +0.06 (-0.21, +0.29) | no |
| m_entry | -0.00 (-0.19, +0.23) | 1.00 | -0.00 (-0.39, +0.29) | yes |
| rvol_entry | +0.04 (-0.17, +0.19) | 1.00 | -0.07 (-0.38, +0.24) | no |
| vix | -0.01 (-0.23, +0.19) | 1.00 | +0.21 (-0.08, +0.54) | no |
| abs_gap | +0.02 (-0.14, +0.15) | 1.00 | +0.02 (-0.29, +0.30) | yes |
| gap_dir | +0.03 (-0.18, +0.25) | 1.00 | +0.00 (-0.28, +0.35) | yes |
| day_move_dir | +0.06 (-0.20, +0.27) | 1.00 | -0.10 (-0.41, +0.10) | no |
| vwap_z_dir | -0.07 (-0.21, +0.10) | 1.00 | +0.11 (-0.18, +0.30) | no |
| rvol20 | +0.05 (-0.14, +0.27) | 1.00 | -0.07 (-0.30, +0.20) | no |
| nth_of_day | +0.04 (-0.13, +0.26) | 1.00 | +0.09 (-0.17, +0.28) | yes |

Pooled (exploratory) every |rho| <= 0.07 with intervals of about +/-0.15. **Power:** the minimal detectable AUC (80% power) is 0.65 backtest, 0.70
forward, 0.62 pooled. The data rule out strong within-setup effects; they cannot see moderate ones.

## Q3 - execution stress on all 166 trades
A delay-0 fill reproduces the engine's ROI for all 166 trades (max difference 0.0).

**S1 spread** (multiple of the model's 0.5% / 0.8% half-spread; full re-run):

| x | 1 | 1.5 | 2 | 3 | 4 | 5 | 6 | 8 |
|---|---|---|---|---|---|---|---|---|
| Mean ROI | +4.6% | +4.3% | +3.9% | +3.2% | +2.1% | +1.2% | 0.0% | -3.2% |
| Profit factor | 1.49 | 1.45 | 1.40 | 1.32 | 1.20 | 1.11 | 1.00 | 0.74 |
| Forward only | -0.2% | -0.4% | -0.4% | -1.0% | -1.3% | -2.0% | -4.0% | -9.3% |

**S2 delay** (strikes fixed at the alert, fill at the then-current ask):

| Delay (min) | 0 | 1 | 2 | 3 | 5 | 10 |
|---|---|---|---|---|---|---|
| Mean ROI | +4.6% | +3.2% | +2.7% | +2.1% | +1.0% | +1.9% |

Slope over 0-5 min: **-0.68 points of ROI per minute (95% CI -1.46 to +0.03)**. At 2x spread plus a 5-minute delay the mean is -0.6%.
Exploratory (not registered): the cost is concentrated in the single-option setup (P1A: -3.5 points at 2 min, -8.3 at 5 min) and P3; the credit-spread
setup (P2) is unaffected.

## Decisions under the registered rules
* **R1** score predicts wins: **no**. The score stays an actionability ranking and must not be read as a forecast.
* **R2** adopt data-fitted factors: **none**. No re-weighting is justified.
* **R3** lateness factor: slope 0.68 is between the 0.3 and 1.0 thresholds, so the shipped lateness curve **stays**. (Its 150 s free window gives away about 1.7 points on average.)
* **R4** tier wording: mean ROI at 2x spread is +3.9% > 0, so the rule does not force a wording change. The bigger caveat is that even at 1x the pooled interval includes zero.

## Post-hoc diagnostics (not registered)
A first leave-one-session-out version was discarded: under a pure-noise null it averaged AUC 0.38, so its 0.32 on real data was an artifact.
Walk-forward replaced it (10-session warm-up; null baseline 0.46 / 0.49 because shuffling without replacement leaves a small negative dependence).

| | Later backtest | Forward | All scored (114 trades) | Null baseline |
|---|---|---|---|---|
| D1 setup score built only from earlier sessions: AUC | 0.50 | 0.32 | 0.44 (0.31-0.58) | 0.46 |
| D2 best refit (module + 11 factors, L2): AUC | 0.42 | 0.50 | 0.48 (0.38-0.60) | 0.49 |

Neither beats noise. A re-fit score would not do better than the one shipped.

## What would settle it
Detecting AUC 0.60 needs about 249 trades (83 more, roughly 16 sessions pooled; a clean forward-only test about 46 sessions). Detecting 0.58 needs 396.
The decisive missing input is real option quotes at alert time: logging the live bid/ask at each alert would turn the model-priced ledger into a real-fill ledger and make the expiry and price-realism factors measurable instead of judged.
