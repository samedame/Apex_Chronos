# Cash sizing, settlement and degraded-data fixes

This change prepares cash-account alert controls without activating them.
`CASH_CONTROLS_ENABLED` defaults to false. The existing live and EOD workflow
definitions, strategy thresholds and base runtime dependencies are unchanged.
No real account file, approved risk limits or encryption credential is created
by this PR. Keep the PR draft and unmerged until reviewed.

The proposed engine fixes apply only to missing/degraded/invalid data. Ordinary
historical decisions must continue to match all 110 original trades and 198
candidates. A frozen 235-message production corpus verifies unchanged default
alert text and quantities on the same normal-data events.

## Sizing and cash controls

The optional cash path is integrated into `LiveSession.dispatch`. It never
falls back to an unguarded entry when a check fails. It requires:

- A current explicitly confirmed cash-account reconciliation and basic-options
  permission. `ACCOUNT_CAPITAL=1500` is never treated as $1,500 settled cash.
- Explicitly approved maximum premium risk per trade, daily loss and account
  drawdown. Pending or missing limits block new entries. This PR does not
  approve the user's pending limits or activate a strategy.
- A causally observed catalog containing the exact standard single-leg
  same-day contract in the model signal. Spreads and missing contracts are
  rejected; no alternate strike or expiry is silently substituted.
- A fresh entry inside the configured grace window, a whole even order size
  of at least two, and enough settled cash for the rounded-up limit premium,
  estimated opening fees and conservative closing-fee reserves.

Quantity is bounded by 25% of the conservative cash-equity basis, available
settled cash, three cash position slots and approved full-premium loss capacity.
Open long options are valued at zero for reserving worst-case loss capacity.
This is deliberately conservative rather than a real-time option valuation.
The $375 budget cannot buy two $200 contracts. A $100 premium receives two
contracts rather than three, and fees/reserves can make an exact $200 balance
insufficient for two $100 contracts.

The protected entry message contains a fixed contract count, exact symbol,
maximum limit price and reservation reference. It labels the price as a MODEL
cap, not a verified live quote. The confidence calculation receives the guarded
quantity. These controls do not establish quote availability or a profitable
edge, and do not replace the separate prospective quote trial.

## Confirmed fills, not model fills

There is no broker account/order API in this change. The bot can constrain its
alert instructions; it cannot prevent an operator from placing a different
order at Robinhood. Explicit operator/broker confirmations are the source of
actual balances and positions. Missing or contradictory records block entries.
Independent activity, deposits, withdrawals and assignments require reviewed
reconciliation rather than an inferred account reset.

Before an entry message is attempted, the cash book reserves its full budget.
The encrypted state is written atomically and flushed to disk. On GitHub Actions
it must also be successfully committed/pushed before the actionable message;
missing durable publication or a failed push blocks the action. An uncertain
delivery keeps its reservation. Delivery-attempt references survive restarts,
so the bot never automatically repeats that order intent. Reconcile/cancel it
explicitly if delivery fails.

Only confirmed BUY records debit actual premium and fees. Only confirmed sales
create net proceeds, unavailable until the settlement calendar's T+1 date,
including bank holidays. An ENTRY/TP1/EXIT model event never credits proceeds or
releases the entry reservation. Only confirmed cancellation releases an
unfilled order's unused budget. Duplicate confirmation IDs are idempotent;
changing a previously used ID blocks the account for reconciliation.

Actual partial fills can produce odd holdings even though order sizes are
even. Reconcile or cancel the outstanding buy remainder before issuing an exit.
Half-sale quantities use actual purchased contracts; no fractional contract is
ever proposed. Exit messages cannot exceed confirmed held quantity. An
unconfirmed exit blocks another sell instruction until its fill/cancellation
is reconciled. A final model exit while another sale is pending therefore
requires manual order/position management. A confirmed manual sale can be
recorded with `manual_exit: true`; it is never generated automatically.

Actual over-limit fills are booked and block further entries. Invalid or
contradictory records also block the book. An operator must correct the record
and explicitly reconcile matching cash and positions. A new session needs a
new confirmation; starting capital is not restored at midnight. Unresolved
overnight holdings block new entries. The book has no automatic exercise or
assignment inference and no brokerage permissions verification of its own.

## Private state and future activation

The cash book is `state/cash-controls.enc`, encrypted with Fernet using a
separately configured `CHRONOS_CASH_BOOK_KEY`. Balances, positions, contract
symbols and fill receipts are never stored as public plaintext. Only encrypted
state can be published. The encryption dependency is optional in
`requirements-cash-controls.txt` and installed for tests; the default live path
does not load it. Missing/wrong keys and corrupt state block the protected path.

Private reconciliation inputs belong outside committed data, for example in
the ignored `.private_cash/` directory. Never put a broker token, encryption
key or plaintext account snapshot in Git. The commands below record already
confirmed facts; neither sends orders or alerts:

```
python -m apex_chronos.cash_controls initialize --book state/cash-controls.enc --input .private_cash/snapshot.cash-confirmation.json
python -m apex_chronos.cash_controls reconcile --book state/cash-controls.enc --input .private_cash/fill.cash-confirmation.json
```

Initialization requires `confirmed: true`, `account_type: "cash"`, `flat: true`,
`options_level2_approved: true`, an aware current-session `as_of`, the actual
`settled_cash` and any `unsettled_proceeds` with their original `sold_at` dates.
Initial allocation cannot exceed the specified $1,500 account. Initialization
refuses to overwrite an existing book. Risk settings use an explicit `approved`
boolean and dollar values `max_trade_risk`, `max_daily_loss`, `max_drawdown`.
No live risk values are supplied in this PR; any future approval/initialization
requires a separate reviewed activation.

Each subsequent record requires `confirmed: true`, a unique `event_id`, an aware
non-future `at` and an `action`:

- `buy` / `sell`: `reservation_id`, exact `symbol`, integer `quantity`, actual
  `price`, and actual cent-valued `fees`. Sales also name their `request_id`.
- `cancel_buy`: the reservation whose remaining buy quantity was confirmed
  canceled. It does not release money spent on filled contracts.
- `cancel_sell`: reservation and exit `request_id` whose cancellation was
  confirmed. It does not change cash or holdings.
- `confirm`: actual `settled_cash`, `unsettled_cash` and a `positions` mapping of
  reservation IDs to held quantities. These must match the persisted book after
  calendar settlement; they cannot reset its balances or invent buying power.

The current GitHub live workflow does not enable this mode or supply its new
key/dependency. Activation needs explicit workflow/runtime configuration and a
reconciled private book. Nothing is enabled by merely opening this draft PR.
If integrated with the separate frozen quote-trial PR, changed engine sources
require a freshly prepared dated trial specification before collecting evidence.

## Degraded-data behavior

Queued P1B entries now reject degraded, missing, short, nonfinite or nonpositive
entry data before slot admission. They produce an explicit SKIP and are not
rescheduled against an invented price. Fresh detector entries reject invalid
entry-price/IV/RVOL values as well.

Held positions do not run TP1, stop, trail or time-stop valuations on flagged or
invalid bars. They remain unresolved and emit a manual-management warning. When
a real bar returns after the deadline, the model exit uses that actual later
minute rather than backdating the fill to the missing deadline. A missing held
ticker still makes the live poll retry; no default value is substituted.

EOD reconciliation excludes tickers with missing/forward-filled final bars or
stale/nonfinite volatility inputs instead of publishing synthetic final marks.
This may change degraded-day reports in a future deployment. It leaves valid
data backtest behavior unchanged.

## Validation

CI runs the full regression suite, full original-backtest parity verification,
and the existing strict cash-shadow catalog/settlement replay. New cases cover
fee-aware even sizing, unconfirmed orders, whole partial fills, holiday T+1,
idempotence, encryption, concurrent reservations, failed delivery/publication,
actual over-limit fills, account mismatch, restart, degraded pending entries,
held deadlines and EOD quality. Synthetic funding fixtures are not prospective
trade evidence or historical executable-price backtests.

Sources: [Robinhood account/settlement restrictions](https://robinhood.com/us/en/support/articles/robinhood-accounts/),
[Robinhood fees](https://robinhood.com/us/en/support/articles/trading-fees-on-robinhood/).
