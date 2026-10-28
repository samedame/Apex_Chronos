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
| P2 Midday Range-Wall Pin | 37 / 68% / +1.5% | 18 / 67% / +1.8% |
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
