# Lab programme: everything the 2026-11-14 research added, as observation-only code

Source: the evidence-graded catalogue of 110 indicators, variables and controls for P2 (project doc
`claude/apex-chronos-lab-programme-2026-11-14.md`). Its conclusion shapes this build: P2 is a same-day variance-risk-premium
carry, no indicator turns it into directional alpha, and the additions that matter are measurement and risk controls.

## Ground rule

**No lab module changes a P2 signal, threshold, exit, ticker, alert byte or the frozen P2 rule.** Changing P2 restarts the
100-trade count that starts 2026-11-16. Each addition therefore runs one of five ways:

| Mode | Meaning |
|---|---|
| LOG | raw data captured for later tests (chain snapshots, NBBO, VIX family, cross-asset, events) |
| CALC | a causal feature computed for every P2 entry and written to `ledger/lab/p2_shadow.csv` |
| FLAG | a pre-declared would-keep / would-mute decision computed next to the real alert; never applied |
| TEST | an archive-replay test or a forward test on the shadow ledger (`docs/LAB_PREREG.md`) |
| RULE | a static guard written as code with a unit test |

Items the evidence rejects are listed with the reason and are not built. Items that need paid data, a human check or
history the repo does not have are marked DEFER with the exact trigger that unblocks them.

## What was built

| Module | What it does | Runs where |
|---|---|---|
| `apex_chronos/lab/indicators.py` | range/vol estimators, variance ratio, efficiency ratio, Noise Area, VWAP bands, volume profile, ATR, squeeze, jump measures, seasonality-adjusted forecast, CUSUM, conformal range bound, etc. | EOD shadow, replay |
| `apex_chronos/lab/options_math.py` | IV solve, touch probability, implied move, VRP ratio, EV per collateral, skew/curvature, GEX, zero-gamma, walls, vanna/charm, spread-as-%-of-credit | EOD shadow, snapshots |
| `apex_chronos/lab/events.py` + `lab/data/events.csv` | FOMC, CPI, NFP, PPI, PCE, GDP, claims, ISM rules, opex, triple witching, VIX expiry, half days, month/quarter end, earnings, ex-dividend flags | EOD shadow |
| `apex_chronos/lab/crossasset.py` | VIX family (VIX1D, VIX9D, VIX3M, VVIX, SKEW, COR1M, VXN, RVX, MOVE), ES/NQ, BTC, yields, DXY, USDJPY, HYG/LQD, SMH | snapshot job |
| `apex_chronos/lab/snapshots.py` | Tradier option-chain snapshots (same-day expiry, greeks, bid/ask, sizes, OI, volume, quote dates) incl. XSP | snapshot workflow |
| `apex_chronos/lab/quote_ledger.py` | NBBO of every P2 leg at alert, +30 s, +60 s, every minute while open, exit and 15:59; staleness; size-unit tags | live loop (thread, never blocks) |
| `apex_chronos/lab/shadow_eval.py` | builds one row per P2 entry with all features and F1-F12 flags; writes `ledger/lab/p2_shadow.csv` | EOD |
| `apex_chronos/lab/stats.py` | session-clustered bootstrap, Holm, CVaR, Diebold-Mariano, risk of ruin, Kelly, vol-target, Bayesian shrinkage | replay, EOD |
| `apex_chronos/lab/killswitch_alt.py` | five alternative kill-switch rules evaluated daily, logged, never applied | EOD |
| `apex_chronos/lab/fills.py` | fill-entry helper and empirical fill/latency model | manual + EOD |
| `apex_chronos/lab/regrade_quotes.py` | regrade archived trades from any NBBO CSV (ThetaData/Massive export) at real mid and taker | on demand |
| `scripts/lab_replay.py`, `scripts/lab_stress.py`, `scripts/lab_snapshot.py` | replay of the pre-registered tests; stress-day harness; snapshot CLI | on demand / workflow |
| `.github/workflows/lab-snapshots.yml` | scheduled chain + cross-asset snapshots at 09:25, 09:45, 10:00, 11:30, 12:30, 13:30, 14:30, 15:30 ET | GitHub Actions |

Full per-item disposition: `docs/LAB_CATALOGUE.md` (generated from `apex_chronos/lab/catalogue.py`; a test checks that
every BUILT item points at a real function).

## What is not possible from this repo today

* **Real historical quotes** (the regrade the research ranked first) need a ThetaData or Massive subscription. The adapter
  reads a CSV export; nothing here pretends to have the data.
* **Stress replays** of Feb 2018, Mar 2020, Aug 2024 and Apr 2025 need bars from those dates; the repo archive starts on
  2026-10-16. The harness is built and tested on a synthetic crash fixture.
* **Tradier option `bidsize`/`asksize` units** still need a human check against a second source. The ledger stores raw sizes
  tagged `size_unit=unverified` and the size-based gates stay off until someone flips one constant.
* **Earnings and ex-dividend dates** come from the Nasdaq public calendar at run time. If that fetch fails the flag is
  `unknown`, never `false`.

## Promotion path

After the Feb-2027 verdict, any flag that passes its pre-registered support criterion can be promoted to a live alert filter
by changing `Settings` (a new pre-registered P2 variant), not by editing this code. Nothing is promoted automatically.
