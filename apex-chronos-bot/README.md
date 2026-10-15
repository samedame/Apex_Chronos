# APEX-CHRONOS alert bot

A GitHub Actions bot that watches the 12 APEX-CHRONOS 1M tickers during the US session and posts **entry, take-profit and exit alerts to Discord**.
It is alert-only: it never places an order. The strategy is the intraday options system from the Master Report (five sub-engines on 1-minute bars,
three capital slots), ported from the original backtest code and **verified against it trade for trade**.

```
09:20 ET  open-scheduler starts chronos-session  ->  loads the last 14 sessions as baselines
09:31-16:00  every minute: fetch completed 1m bars -> SessionEngine.step(m) -> Discord alerts (ENTRY / TP1 / EXIT)
16:25 ET  chronos-eod re-runs the day on final bars, reconciles live vs final, appends ledger/trades.csv, posts the forward record
```

## What an alert looks like

```
🟢 APEX-CHRONOS ENTRY - NVDA P2 · slot 1/3
SELL 218 CALL / BUY 223 CALL (call credit spread)   exp TODAY (0DTE) - the backtest assumed a same-day expiry
Signal: Phase2_MiddayPin_CreditSpread · bar 10:49 closed 10:50 ET · underlying 215.16
Model credit $0.92/share on a $5-wide spread -> collateral (max loss) $4.08/share ($408/contract). ROI below is measured on the collateral. RVOL 3.29 · IV(model) 47%
Sizing: slot budget $6,250 (25% of $25,000) -> 15 contract(s) at model $408 each.
Stop: buy the spread back if it costs >= $1.74 (credit + 20% of collateral), judged on a 1-min close · take HALF off with a limit buy-back at $0.27 ...
```
```
🎯 TP1 - sell HALF - MSFT (Phase1A_GapTrap_Single0DTE) · bar 09:43 closed 09:44 ET · model ROI touched +91% · slot 3
🟢 PROFIT-TRAIL EXIT - MSFT (Phase1A_GapTrap_Single0DTE) · exit bar 09:43 closed 09:44 ET · slot 3
close the REMAINING half now. Model result: +77.9% on cost (peak +91%) after 7 min
```
(Examples are from a replay of recorded 2026-09 data.) All prices are model prices; compare with your live bid/ask before acting.

## Does it do what the backtest did?

Yes, and this is checked rather than assumed. `python -m verification.verify_against_backtest` runs the **original research code** (`verification/reference/`, unmodified, pinned by SHA-256)
and the bot on the same data and compares them:

| Check | Result |
|---|---|
| 110-trade ledger: ticker, date, module, direction, entry minute, exit minute, exit reason, slot, cost, ROI | identical, largest numeric difference 0.0 |
| Every pre-scheduler candidate (198) and every feature array (VWAP, EMA, noise area, RVOL, IV) | identical |
| 24-ticker cross-check (364 candidates -> 131 trades; scheduler under pressure) | identical |
| Live mode: each minute sees only the bars up to that minute (no look-ahead) | identical |
| Late polls that catch up 3 / 7 / 15 / 45 minutes at once; cold restarts at 12 points in each of 21 sessions | identical |
| 20 perturbed markets (prices, gaps, volume, VIX), 8,158 candidates -> 2,814 trades | identical, zero differences |
| 20 deliberately broken bots (wrong threshold, slot rule, exit rule, look-ahead, stale IV ...) | all 20 caught |

Full results and the live-vs-backtest sensitivities: [`verification/VERIFICATION_REPORT.md`](verification/VERIFICATION_REPORT.md).
It runs offline in CI on a trimmed copy of the data (`verification/fixtures.tar.gz`) and again here on the full data folder.

## What it does not prove

The bot reproduces the backtest; it cannot make the backtest better than it was. 110 trades over 21 sessions, thresholds fitted on the same data, **Black-Scholes model prices instead of real quotes**,
every ticker assumed to have a same-day expiry, no fees in the simulator. Read [`docs/LIVE_VS_BACKTEST.md`](docs/LIVE_VS_BACKTEST.md) before sizing real money,
and run it as a shadow forward test first.

## Setup

[`docs/SETUP.md`](docs/SETUP.md): repo, secrets, `open-scheduler` jobs, smoke tests, shadow-week checklist, and the Actions-minutes cost note (the loop runs ~6 h a day: free in a public repo, ~$30-36/month in a private one).
Strategy reference: [`docs/STRATEGY.md`](docs/STRATEGY.md).

## Layout

```
apex_chronos/    signals.py (5 detectors) · engine.py (scheduler) · position.py (exit ladder) · pricing.py · features.py
                 data.py (Yahoo) · history.py · runner.py (live + EOD) · alerts.py · state.py · replay.py (backtest feed) · calendar_nyse.py
verification/    reference/ (original code, SHA-pinned) · verify_against_backtest.py · fuzz.py · fixtures.tar.gz · VERIFICATION_REPORT.md
tests/           unit tests + golden files (the original code's output on the fixtures)
state/ ledger/   written by the bot: sent-alert ledger and session history · the forward model-trade record
.github/workflows/  chronos-session.yml · chronos-eod.yml · tests.yml
```

Commands: `python -m apex_chronos live | eod | once | replay | selftest`.
