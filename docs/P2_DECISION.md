# Phase 2: the keep-or-retire rule

Frozen 2026-11-13, before any trade it will be judged on existed. The numbers below are also in
`apex_chronos/phase_review.py` (`RULE`) and pinned by `tests/test_phase_review.py`, so changing one shows up as a diff.
Changing the rule after the first new trade restarts the count.

## Why a rule is needed

Forward record, 15 October to 12 November 2026 (21 sessions, 120 model trades, mean -1.5% a trade). Every figure is a
Black-Scholes model price, not a fill.

| Phase | Trades | Mean ROI a trade | Winners | Decision |
|---|---|---|---|---|
| P1 (gap modules) | 74 | -3.4% | 32% | Alerts muted, still logged |
| P2 (midday credit spread) | 40 | +3.3% | 72% | Alerted, on the rule below |
| P3 (afternoon call spread) | 6 | -10.3% | 33% | Alerts muted, still logged |

P2 was picked out because it was the best of three phases on this same record. That is selection after the fact, so
those 40 trades cannot also be the proof. Its backtest figure was +1.5% over 37 trades, the weakest phase there. The
audit of the whole bot found no rule that survived a multiple-testing correction.

## The rule

| | |
|---|---|
| What counts | P2 model trades in `ledger/trades.csv` dated **2026-11-16 or later**, in ledger order |
| Sample size | **100 trades.** The verdict is taken on exactly the first 100, not sooner and not later |
| KEEP | mean ROI **at least +1.0%** of collateral **and** the one-sided 95% lower bound of the mean **above 0** |
| RETIRE | anything else at 100 trades |
| Stop early | the new trades sum to **-100% of collateral or worse** at any point: RETIRE without waiting for 100 |

The lower bound is a bootstrap that resamples whole sessions (10,000 draws, seed 20261113), so several P2 trades on
one day are not treated as independent.

**What each outcome means.** KEEP: P2 stays a paper alert and becomes eligible for the real-quote gate in
`docs/QUOTE_TRIAL.md`. It is not a signal to fund it. RETIRE: P2 alerts are muted like P1 and P3 from the next
session, automatically; the ledger keeps recording all three phases.

## Where the numbers come from

- **100 trades.** P2's per-trade standard deviation on the forward record is 9.7%. Pooling the backtest (+1.5%, 37
  trades) with the forward record (+3.3%, 40 trades) gives about +2.5% a trade. Detecting an edge of that size with a
  one-sided 5% test and 80% power takes (2.49 x 9.74 / 2.46)^2 = 97 trades, rounded to 100. If the true edge is zero,
  the rule keeps P2 at most about 1 time in 20.
- **About when.** P2 has fired 1.9 times a session, so 100 trades is roughly 53 full sessions: early February 2027.
- **+1.0% floor.** Model prices assume a half-spread of 0.5% to 0.8% of the option's value and no fees. Crossing a
  real two-leg spread twice costs about $4.00 a contract on the SPY prior in `trading-ops/config/cost_priors.json`
  (median entry $2.50 plus median early exit $1.50), which is 0.8% of P2's median $526 collateral. Single-name
  same-day spreads are usually wider than SPY, so this floor is a minimum, not an estimate of real cost.
- **-100% stop.** With a 9.7% standard deviation, a true edge of +2.5% a trade almost never drifts that low; a
  strategy losing 2% a trade gets there in about 50 trades.

## Limits

- Model prices only. A KEEP here says the model record held up, not that real quotes would have.
- One market regime (autumn 2026 into winter).
- Most single names do not list a same-day expiry every weekday; the model prices one anyway.
