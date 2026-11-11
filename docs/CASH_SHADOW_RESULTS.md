# Initial cash-shadow evidence — November 11, 2026

These are theoretical option-price simulations, not broker fills. Parameters
were frozen before evaluating these results; no threshold was retuned to them.

## Historical coverage

| Window | Sessions | Qualified P1A candidates | Catalog-backed candidates | Funded trades |
|---|---:|---:|---:|---:|
| September 16–October 14 | 21 | 48 | 0 | 0 |
| October 15–November 10 | 19 | 46 | 0 | 0 |
| Combined history | 40 | 94 | 0 | 0 |

No option catalogs were captured before these historical signals. The strict
replay therefore skipped them. $0 PnL, $0 fees, no slippage paid and 0% drawdown
in every historical scenario reflect **no exposure**, not profitability.
Today's catalog was not applied backwards. All 40 sessions were previously
studied; none is a new holdout.

## One catalog-backed model replay

For November 11, use the already archived November 10 evening public Cboe
snapshots, conservatively observed at 03:35 UTC November 11. Original acquisition
times and payload hashes were verified. The new Nasdaq directory download at
18:27 UTC was deliberately unavailable to earlier signals. This retrospective
replay covers the complete 09:30–10:55 ET P1A entry/management window using
completed underlying bars, not the rest of the unfinished trading day.

All four qualifying candidates had listed contracts. IWM, QQQ and AMZN were
funded in each independent slippage scenario; META's selected strike was too
expensive for two contracts within the slot budget. The account starts at
$1,500 fully settled, with a 25% per-slot limit.

| Extra slippage per share/side | Net PnL | Return | Ending equity | Robinhood fee estimate | Extra slippage paid | Max intraday drawdown |
|---|---:|---:|---:|---:|---:|---:|
| $0.00 | +$69.16 | +4.61% | $1,569.16 | $0.84 | $0.00 | 20.40% |
| $0.01 | −$238.83 | −15.92% | $1,261.17 | $0.83 | $20.00 | 24.89% |
| $0.03 | −$298.67 | −19.91% | $1,201.33 | $0.67 | $48.00 | 19.91% |
| $0.05 | −$330.67 | −22.04% | $1,169.33 | $0.67 | $80.00 | 22.04% |

The zero-extra-slippage result already includes modeled spread, cent rounding
and fees. Extra slippage affects affordability and exit decisions throughout
the replay. It is not merely deducted at the end. IWM reaches the half-sale
target with no additional slippage, but misses it with one cent of additional
slippage and eventually time-exits; at three cents it stops out early. Drawdown
can therefore be larger in a less expensive scenario with a different path.

With no extra slippage, IWM contributes +$275.50, QQQ −$98.17 and AMZN −$108.17.
Settled cash ends at $563.60; $1,005.56 of net proceeds remain unavailable until
November 12. The other scenarios also carry all sale proceeds to that settlement
date. Cash flows reconcile, all fills are whole contracts and all accounts end
flat.

## Interpretation

This demonstrates catalog timing, long-option cash accounting and cost-sensitive
simulation. It does not establish a tradable edge. The historical listing gap,
modeled prices, one retrospective session and substantial drawdowns leave the
strategy unsuitable for live promotion on this evidence. Keep collecting
prospective shadow data without changing alerts.

See [cash-shadow rules and verification](CASH_SHADOW.md). The public collector
test found 11,208 eligible listed series across all 12 requested tickers. It
does not verify Robinhood approval, actual holdings or executable quotes.
