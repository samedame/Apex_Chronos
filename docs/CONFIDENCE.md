# Entry-alert confidence score

Every ENTRY alert now carries a 1-100 score directly under its headline:

```
Confidence 75/100 (setup 45 · execution 80) - tier B, verify the flagged item before acting
Setup record - Gap-Trap Sweep: won 45% of 75 tracked trades (backtest 51%/47, forward 36%/28), mean +7.3%/trade. A record, not a forecast.
Biggest drag: same-day expiry NOT verified against a real chain - may not exist for AMD today
```

TP1 and EXIT alerts are not scored: they are management calls on a position you already hold, not a decision to take or skip.
A stale ("Missed entry") alert is not scored either; it is not actionable.

## What the number is, and is not

It ranks **how actionable this alert is** and **how well its setup type has done**. It is **not a probability that the trade wins.**
That is not modesty; it was measured. Scoring all 165 tracked trades as if each alert had arrived on time:

| Sample | Trades | Spearman(score, ROI) | Tier A mean ROI / wins | Tier B mean ROI / wins |
|---|---|---|---|---|
| Backtest | 110 | -0.10 (p 0.31) | +1.3% / 50% | +9.8% / 61% |
| Forward ledger | 55 | +0.01 (p 0.91) | -5.5% / 41% | +2.1% / 45% |

Tier A here is the index ETFs (SPY/QQQ/IWM), the only tickers where a same-day expiry is known to exist, and they have had the weakest *model*
returns. The single-name results rest on an assumed 0DTE expiry that may not be tradeable (`docs/LIVE_VS_BACKTEST.md` section 3), so a higher model
return there is not evidence that those trades could have been taken. Treat tier D as "do not act as written", never as "this trade will lose",
and tier A as "nothing about the alert itself stops you", never as "this will win".

## The two components

| | Weight | What it measures |
|---|---|---|
| **Setup** | 15% | The setup type's own record (P1A, P1B, P1C, P2, P3): win rate and mean ROI per trade, pooled over the 110 backtest trades and the live forward ledger, shrunk toward the pooled average (40 pseudo-trades) so a handful of trades cannot dominate. |
| **Execution** | 85% | Whether the alert can be acted on as written. |

Execution factors (weights are judgement, renormalised over the factors that apply):

| Factor | Weight | Scoring |
|---|---|---|
| Same-day expiry | 40% | SPY/QQQ/IWM = 1.0 (they list daily). Any other ticker = 0.5 and is flagged: the bot does not check the real chain. A chain check that says no expiry exists = 0 (disqualifying). |
| Lateness | 30% | Full marks inside the grace window (150 s); falls linearly to 0.3 at 10 minutes; older than 10 minutes is 0 (disqualifying; the bot would not send it as an entry anyway). |
| Sizing | 20% | 2+ contracts = 1.0, exactly 1 = 0.85, 0 = disqualifying. If `ACCOUNT_CAPITAL` is unset it is not scored (identical on every alert, and the alert already says so). |
| VIX-feed freshness | 10% | The normal ~15-20 minute Yahoo lag is not penalised; 20-30 minutes falls to 0.5. Measured effect of the normal lag is small (mean ROI +7.01% -> +6.60%). |

A disqualifying factor caps the alert at 49 (tier D) however the average works out. A missing input scores a declared neutral (0.5), never a silent 1.0.

Tiers: **A** 85-100 actionable as written · **B** 70-84 verify the flagged item before acting · **C** 50-69 reduce size or paper this one · **D** 1-49 do not act as written.

## Why the setup component is only 15%, and why three setups are "too thin"

Reproduce everything below with `python scripts/refit_confidence.py --as-of 2026-10-28 --checks`.

| Setup | Backtest (n / win / mean) | Forward (n / win / mean) |
|---|---|---|
| P1A Gap-Trap Sweep | 47 / 51% / +11.0% | 28 / 36% / +1.1% |
| P1B Confirmed Gap + ORB15 | 7 / 57% / +14.9% | 2 / 50% / +0.5% |
| P1C ORB15 Bull-Trap Reject | 13 / 54% / +4.5% | 4 / 0% / -17.7% |
| P2 Midday Range-Wall Pin | 37 / 68% / +1.5% | 18 / 67% / +2.1% |
| P3 Afternoon Vanna Squeeze | 6 / 50% / +6.1% | 3 / 33% / -2.0% |

* The backtest said P1A was the engine (+11.0% vs P2's +1.5%). The forward ledger did not repeat that: P1A fell to +1.1% with a 36% win rate while P2
  held steady at 67% wins. After shrinkage the two established setups score within five points of each other, so giving setup type more weight
  would add noise, not information.
* P1B, P1C and P3 have 9, 17 and 9 pooled trades. Ranking them on that would be reading tea leaves, so they score a flat 40 and the alert says why.
  (P1C's forward record is 0 wins in 4; that is flagged by the printed record, not by a made-up penalty.)
* Within a setup type, RVOL at entry, entry minute and the bot's own rank score do **not** predict ROI (|rho| <= 0.22, p >= 0.39, and the sign
  changes between the backtest and forward samples). They are deliberately not used.
* The backtest's thresholds were chosen on the same data, and every price is a Black-Scholes model price, so the backtest side of this table is
  optimistic. The forward ledger is also model-priced (final-bar replay), so neither side reflects real fills.

## Keeping it honest

* `RECORD` in `apex_chronos/alert_confidence.py` is a snapshot as of `AS_OF`. Re-run `scripts/refit_confidence.py` to refresh it; the tests pin the
  backtest side to the bot's own replay and the forward side to `ledger/trades.csv` up to `AS_OF`, so a stale or mistyped table fails CI.
* The score is stored with each posted entry in `state/sent-<date>.json` (`conf`), so it can later be checked against realised outcomes.
  Do that before putting any weight on it: the table above is the bar it has to beat.
* The single largest improvement available is a real option-chain check for the same-day expiry. Pass its result as `expiry_verified` and the
  expiry factor stops being a guess in either direction.
* A scoring failure never blocks an alert: the live loop catches it, logs it, and sends the alert without the score line.

## Expected-return shadow test

The corrected evidence snapshot reports P2's forward mean as +2.11%. Live alert
calibration uses `ALERT_RECORD`, which preserves the deployed +1.80% input and its
printed setup record. This keeps existing confidence scores and alert text identical
while the corrected `RECORD` continues to be checked against the ledger.

The end-of-day job writes `ledger/shadow/YYYY-MM-DD.json` with every candidate that reached
the scheduler on the final bars, including candidates neither policy selected. Each record has
the original rank score, the shadow estimate, and the slot and modeled outcome for each policy
that took it. A null policy entry means it did not select that candidate.

The shadow policy ranks same-minute candidates by mean ROI for their ticker and setup in the
forward ledger from strictly earlier sessions. Each ticker/setup mean is shrunk toward its setup
mean with 20 pseudo-trades, and each setup mean toward the pooled mean with 40. Ties keep
the original order. Both policies have their own positions and slot availability and use the
same phase limits, entry rules and exits. Live alerts and the Discord summary are unchanged.

Compare `delta_sum_model_roi` across days and use `current_only` and `shadow_only` to see when
the selection actually changed. The delta is shadow minus current in summed trade ROI, as a
decimal, and is null if either replay ended with an open position. The training ledger contains
trades picked by the original scheduler. Both the score and the reported outcomes use model
prices before fees. They do not verify real expiries, quotes, fills, or net profitability.

The same daily report also contains a `skip_p1c_p3` policy. It uses the original
candidate priority but rejects P1C and P3 before either can occupy a slot or consume
a phase allowance. It replays its own positions, so other candidates can use the
freed capacity. Candidate records retain excluded setups with a null selection
under this policy. Its summary gives trade count, mean and summed model ROI,
selection changes, and summed ROI difference from the current scheduler.
This policy is independent of the expected-return ranking and runs only at end
of day. Since the excluded setups were chosen after reviewing their losses, future
daily comparisons are needed to evaluate the filter beyond that reviewed sample.

The independent `p1a_rvol1_retrace1` policy changes only P1A's post-signal filters
to RVOL <= 1.00 and retrace < 1.00. The entry-minute ceiling stays at 35 and all
other setups, priorities, sizing assumptions, and exits retain their defaults.
The overrides belong to that shadow engine instance; the live engine uses its
existing limits. This policy replays its own slots and phase allowances.

The shared candidate log includes the union of baseline and P1A-policy candidates,
so a newly admitted candidate is recorded even if the current bot never considered
it. The policy summary includes selections, mean and summed model ROI, and the
difference from the current scheduler. These limits were selected from the
historical optimization search, so future comparisons are required to validate them.

The `combined_p1a_skip_p1c_p3` policy applies both changes in one independent
end-of-day replay. It uses P1A RVOL <= 1.00 and retrace < 1.00, excludes P1C and P3,
and retains the original ranking, phase limits, and exits. Its own slot availability
can admit trades that neither separate policy selected. The daily report records
its selections alongside the current scheduler and both separate policies, plus
mean and summed model ROI and the difference from the current scheduler.
Live defaults and alert formatting are unaffected.

## Execution costs, account sizing and drawdowns

Each daily report adds an `execution_costs` block for the current scheduler,
P1C/P3 exclusion, P1A-only and combined policy. The expected-return ranking retains
its existing gross comparison. Each of the four account comparisons has sixteen
scenarios, crossing additional slippage of $0.00/$0.01/$0.03/$0.05 per option share,
per leg, per side with four sizing modes (64 accounts total).

* `fractional_equivalent` is an idealized model that targets the slot budget and
  permits exact half exits.
* `whole_even_contracts` requires an even number of at least two contracts, so the
  original half-at-TP1 rule can be followed. Unfunded signals are counted as skipped.
  They retain their paper scheduler slots and do not generate replacement trades.
* `whole_contracts_keep_runner` allows any affordable integer quantity, minimum
  one. At the first profit target (TP1), it sells floor(quantity / 2) and keeps the
  rest for the existing trailing or time exit. A single contract stays open through
  TP1. The remaining contracts earn the unblended runner return, rather than the
  original paper trade's equal-half average.
* `whole_contracts_sell_odd_at_tp1` uses the same integer entry sizing but sells
  ceil(quantity / 2) at TP1. A single contract closes entirely at TP1. The account
  records its actual exit minute and retains the original paper exit and ROI for
  comparison. No extra fee or sale is recorded when the later paper exit arrives.

One-contract modes can buy more than one contract when the budget permits. For
spreads, the quantity refers to complete spreads and costs include every leg.
An early account exit keeps its original paper scheduler slot occupied until the
paper exit; cash released earlier can affect the size of later selected entries.
Neither funding skips nor early account exits create replacement signals.

Starting capital uses `ACCOUNT_CAPITAL`, with a $1,500 fallback. Allocation uses
`SLOT_FRACTION`, default 25%. Each entry targets that fraction of the prior minute's
net liquidation equity, including entry costs and constrained by available cash.
The report gives daily and cumulative PnL, fee estimates, additional slippage,
funded/skipped counts, individual funded trades and drawdowns. Equity is marked
each minute using modeled liquidation proceeds after estimated remaining closing
costs. Intraminute drawdowns and actual broker marks can differ.

`execution_costs.contract_sizing_comparison[policy][slippage]` summarizes the three
whole-contract modes side by side, including daily/cumulative PnL, fees, slippage,
drawdowns and funding counts. `differences_vs_two_contracts` gives the one-contract
variants minus `whole_even_contracts`. Positive PnL differences mean more profit;
positive drawdown differences mean a larger drawdown. The detailed account and
trade records remain in `execution_costs.policies`. `sizing_rules` describes each
mode, and single-contract/odd-lot entry counts make changes in exposure visible.

The fee model is pinned as of November 10, 2026 for US retail stock/ETF options.
It includes $0 commission, ORF/OCC, SEC sale fees, CAT and date-specific TAF,
including the October-December 2026 pause. Fees on spread sales use model leg
premiums as an estimate of sold principal. Rates and execution rounding can change.
Slippage is an additional sensitivity beyond the existing modeled spread.
The assumptions and source URL are embedded in each report.
[Robinhood fee guidance](https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/)

Account tracking starts a new cohort on the first cost report and when capital,
allocation, universe or strategy/model source changes. The reset reason and cohort
date are explicit. Compatible reports carry equity, high-water marks and drawdowns
from the preceding full session. Same-day reruns ignore their own saved ending
balance, and future reports are never used. Parent hashes detect changes to earlier
reports. A gap, failed predecessor or inconsistent history produces an explicit
cost error until the affected sessions are replayed, preserving the gross report.
Cost failures do not modify Discord output or suppress the usual end-of-day work.

The sizing comparison uses schema version 2 / `robinhood_shadow_accounts_v2`.
Upgrading a saved v1 report starts all 64 accounts together at the configured
capital on the first v2 session, with `reset_reason: settings_changed`; v1 ending
balances are not mixed with newly introduced accounts. Earlier files remain
saved. Later compatible v2 sessions compound normally. Historical one-versus-two
reconstructions are separate analyses, not the starting balances of this cohort.

These are modeled account overlays. Historical chains, expiration availability,
broker approvals, actual fills and account restrictions are not verified. The
existing live scheduler, sizing messages and confidence calibration are unchanged.
