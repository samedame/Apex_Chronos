# Cash-only listed-options shadow

This independent experiment models a $1,500 Robinhood cash account. It writes
`cash_only` in `ledger/shadow/YYYY-MM-DD.json` after the existing end-of-day
Discord message. It never sends an alert or places an order. The live engine,
live sizing, alert text and existing 64 policy/sizing/cost accounts are unchanged.
Cash-shadow errors are saved in this block without suppressing the old report.

## Frozen v1 rules

- P1A only: the original first-trigger detector, entry minute at most 35,
  cumulative RVOL at most 1.00 and retrace strictly below 1.00. All other setups,
  including every spread setup, are excluded.
- Buy calls or puts in the signal direction. Select the earliest catalog-listed
  expiry from the session date through seven calendar days later, then the
  actual listed strike nearest modeled absolute delta 0.45. Ties use distance
  from spot, then symbol. Do not select a cheaper farther-away strike just to
  afford a trade.
- Three concurrent slots, at most four funded entries per day, original signal
  score and ticker order. Unfunded candidates do not occupy slots. Entry
  selection occurs before that minute's exits.
- Budget at most 25% of preceding-minute modeled equity, capped by settled cash
  after existing closing-fee reserves. Buy even quantities of at least two
  contracts. Initial $1,500 is assumed fully settled, with no other holdings,
  deposits or withdrawals. This is not a broker balance check.
- Model option value with Black-Scholes and the existing causal IV proxy,
  using the actual listed expiry through 16:00 ET and ACT/365 time. Include the
  existing ticker spread assumption and adverse cent rounding. Independently
  simulate additional slippage of $0, $0.01, $0.03 and $0.05 per option share per
  side, equivalent to up to $10 per contract round trip beyond that spread.
- Manage at completed minute-close bid marks. Stop at -26%; at +90% sell half
  the integer contracts, then apply a 54% gain floor and a 72%-of-peak gain
  trail. Exit all remaining contracts after 50 minutes. A gap exits at the
  current modeled bid, not a more favorable crossed threshold.

## Listing evidence and collection

An independent pre-open workflow archives the downloadable
[Nasdaq options series directory](https://www.nasdaqtrader.com/dynamic/symdir/options.txt)
twice on weekdays (12:05/13:05 UTC). It carries no Discord credential and does
not dispatch the live or EOD workflows. It becomes scheduled only after merge.

The [directory specification](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs)
defines actual roots, calls/puts, strikes, expiries and pending status. The
collector keeps requested equity/ETF roots matching their underlying, active
`Pending=N` rows and expiries within seven days. Common adjusted roots and
pending additions are excluded. Root equality is a standard-contract filter;
it is not an independent corporate-action deliverable verification. The model
assumes standard 100-share contracts; unsupported deliverables are excluded
when identifiable. This venue directory may omit contracts available elsewhere.

Compressed snapshots record actual collection completion time, publisher date,
source and content digest. A snapshot must have been observed by signal close
and be no more than 24 hours old. Future or stale catalogs are never used;
missing listings cause an explicit skip. A late or failed collection therefore
does not guess the contract. Publisher time is never substituted for observation
time. Public directory membership establishes neither Robinhood availability,
approval, liquidity nor executable prices.

Manual collection: `python -m apex_chronos.cash_catalog --state-dir state`.
The directory stays local unless the existing explicit `APEX_GIT_PUSH=1` switch
is set. Catalog commits use `[skip ci]`, as with other daily state.

## Cash, fees and drawdowns

Entry premiums and fees immediately debit settled funds. Net exit proceeds,
including TP1, remain unavailable until T+1 settlement. The calendar excludes
NYSE closures and Columbus/Veterans bank holidays, when trading may remain
open but settlement does not occur. Early closes can still settle. For example,
a November 10, 2026 sale settles November 12, not Veterans Day. Coverage is
explicitly limited to 2026-2027; update dates and rates before extending it.

The fee estimate follows Robinhood's nonprofessional US equity/ETF-option
schedule checked November 11, 2026: no broker commission, $0.04 per contract per
side ORF/OCC, CAT rounding, sale-principal SEC fees and sold-contract TAF with
nearest-cent rounding and the October-December 2026 pause. These new accounts
correctly round a two-contract $0.00658 TAF to $0.01 outside the pause. Existing
shadow fee models are untouched. Rates may change; this is a dated estimate.

Reports include each candidate's selection/skip reason, contract provenance,
whole-contract fills, fees, slippage, settled and queued proceeds, minute equity,
daily and cumulative PnL and peak-to-trough drawdowns. Equity includes unsettled
cash and open options marked at modeled liquidation value net of exit fees.
Settlement only changes spendability, not equity. Four slippage scenarios carry
independent balances. Each report hashes its preceding cash report and verifies
all ancestors, flatness and accounting. A missing or changed parent fails closed;
changed model/settings start an explicitly identified new cohort. Reruns ignore
same-day and future reports. Early-close no-trade dates are omitted from cohorts,
while cash maturities are released at the next modeled session.

## Historical interpretation and verification

Historical underlying bars do not contain historical option listings or quotes.
Do not apply today's directory backwards. Without causal catalogs, the strict
replay logs qualified candidates and skips them: zero PnL then demonstrates no
exposure, not a profitable strategy. Even a catalog-backed replay uses theoretical
prices, not real fills, and cannot establish executable profitability. All prior
research sessions have already been inspected; none is an untouched holdout.

Run `python -m pytest -q tests`, the original full
`python -m verification.verify_against_backtest --data-dir verification/fixtures.tar.gz --fuzz-seeds 4 --workers 2`,
and `python -m verification.verify_cash_shadow --data-dir verification/fixtures.tar.gz`.
The new verifier also accepts `--catalog-dir state/cash_options`. CI runs all
three. Tests cover future/stale/corrupted catalogs, actual expiry/strike selection,
integer exits, unaffordable entries, unavailable proceeds, bank holidays, fees,
cash reconciliation, drawdown carry, parent integrity and EOD isolation. Continue
prospective catalog collection before considering this for live alerts.

Sources: [Robinhood accounts](https://robinhood.com/us/en/support/articles/robinhood-accounts/),
[T+1 settlement](https://robinhood.com/us/en/support/articles/T1-settlements/),
[fees](https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/),
[DTC 2026 settlement holidays](https://www.dtcc.com/-/media/Files/pdf/2025/10/15/23036-25.pdf),
[standard equity option unit](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications).
