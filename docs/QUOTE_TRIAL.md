# Quote capture and a frozen prospective paper trial

The initial configuration is **observation-only**, as requested. Tradier is the
selected data connection. Risk limits remain unapproved and no paper entries
are funded. Collecting quote observations does not advance the 60-session or
100-trade evaluation counters. No live alert or broker order is sent.

## What runs independently

`quote-paper-trial.yml` is separate from the live session and receives no Discord
credential. It uses the existing `TRADIER_API_KEY` and a separate
`CHRONOS_QUOTE_ARCHIVE_KEY`. Production market-data GET requests are the only
Tradier operations implemented; account and order endpoints are absent. An
Alpaca OPRA adapter is also available but is not the configured provider.

The weekday backups are 13:20 and 14:20 UTC. The wrong DST offset exits before
the collection window. A completed session is not replaced by a late backup.
GitHub schedules and runner startup can be delayed; the collector records actual
observation times and incomplete coverage. It never invents missed quotes. The
same command can run locally if more reliable timing is available.

Observation mode records a 90-minute window beginning around the open. A late
start can collect later market observations for up to 90 minutes or the close;
these observations do not become a complete P1A evaluation session. A funded
evaluation requires coverage before the open through at least 11:00 ET and can
continue until 11:30 to seek actual quote-supported exits.

Each poll records provider, requested feed, option symbol, exchange timestamps,
local receipt time, both prices, both displayed sizes, exchanges and available
condition metadata. Tradier's bid/ask timestamps are retained separately. Its
quotes API does not provide OPRA condition codes; this is recorded as
`not_supplied`, never synthesized as a verified regular condition.

Reference calls and puts for the 12 tickers are captured alongside both arms'
selected contracts. First-trigger P1A candidates are logged before cash, slot,
risk or quote-size decisions. All underlying inputs use completed bars and
strictly earlier saved baselines. Contract selection retains the cash shadow's
earliest listed expiry within seven days and modeled delta target of 0.45.

## Data quality and storage

Both bid and ask must be no more than 15 seconds old, with receipt time no later
than the decision. Crossed markets, zero-sided markets, wide spreads (over 20%
of ask), unknown size units, unsupported conditions and substitute feeds cannot
support a qualified fill. Delayed/sandbox quotes cannot silently become current
OPRA evidence. Missing fields remain missing or cause a failed validation.

Alpaca requests explicitly name `feed=opra`; its modified free indicative feed
is rejected. Tradier uses only `api.tradier.com`, never the sandbox host. Quote
size units must be confirmed in the frozen configuration, or reported as a
one-contract lot by the provider. Raw reported sizes are preserved; no assumed
100-times conversion inflates tradable quantity. Unknown units remain excluded.

Raw quotes, exact contracts, candidates and fills remain in authenticated Fernet
encrypted archives. Plaintext exists only in process memory. The repository and
workflow artifacts receive encrypted archives and public aggregate metrics.
The archive key must be kept separately and backed up; it is never committed,
printed, added to an artifact or passed as a command-line value. Changing the
key without retaining the old key makes old captures unreadable.

The optional encryption dependency is pinned in `requirements-quotes.txt`.
The live runtime's `requirements.txt` stays unchanged. Public daily status can
be read without loading encryption code or accessing data credentials.

## Prospective trial specification

`trial/cash_quote_trial.json` records a dated source/dependency fingerprint,
future earliest evaluation date, $1,500 capital, rules, benchmarks, cost cases
and review schedule. It contains no secrets. Source changes cannot silently
continue an existing trial. Reports form a checked chronological parent chain;
same-day and future reports are ignored when restoring the preceding balance.

The two arms are:

| Arm | P1A cumulative RVOL cap | Retrace ceiling | Other setups |
|---|---:|---:|---|
| Candidate | 1.00 | strictly below 1.00 | excluded |
| Original-P1A cash control | 1.25 | strictly below 0.75 | excluded |

The control is the original P1A signal translated to the same cash-account
execution rules. It is not the complete existing bot, whose spread setups are
ineligible for this cash account. A second benchmark leaves cash untraded and
assumes zero trading return. Both trading arms receive equal capital, costs,
cash restrictions, quote timing, sizing, slots and management.

The primary case adds $0.01 per option share per side. Predeclared sensitivities
use $0/$0.03/$0.05 additional slippage, 60-/120-second entry delays and a
deterministic missed-first-entry case. There is no search for the best case.
Price changes, delays and missing quotes affect the actual simulated entry and
exit path rather than only an end-of-day cost subtraction.

After risk limits are approved and data access/size units are verified, prepare
a fresh dated activation specification before its first evaluation session.
Prior observation-only captures, probes and fixtures never count retroactively.
If any evaluated trial report already exists, changed rules or limits require a
new trial ID and a fresh future period. Approval is not inferred from a proposed
number or from the passage of time.

## Prepared account and risk controls

These controls are implemented and tested, but funding is disabled while limits
are pending:

- Buy even quantities of at least two contracts, within 25% of preceding-poll
  equity, available settled funds and the approved risk budget. A smaller risk
  cap can make the strategy unable to trade; no farther-OTM strike is selected
  to bypass the cap.
- Limit maximum premium plus opening fees and closing reserves per trade.
  Reserve loss capacity for all open long options falling to zero before
  admitting another entry. Apply daily-loss and account-drawdown dollar limits,
  three slots, four daily entries and one entry per ticker.
- Debit entry cash immediately; net sales remain unsettled until the correct
  T+1 settlement date, including bank holidays. Proceeds cannot fund a same-day
  replacement.
- Simulate buys at observed ask plus adverse slippage and sells at observed bid
  minus adverse slippage. A fill cannot exceed remaining displayed capacity.
  Re-polling a quote, or updating only its other side, does not replenish that
  side's size. Each account is an independent alternative, not simultaneous
  orders sent to the market.
- Evaluate stop/target/trail decisions at minute boundaries using observed
  executable bids. Stop at -26%; sell half at +90%, then use the original 54%
  gain floor and 72%-of-peak trail after the half-sale completes. The holding
  deadline is 50 minutes from the actual simulated entry. Partial exits remain
  whole contracts and pending exits seek new displayed liquidity.
- Missing or insufficient bid capacity receives a conservative zero value for
  the unsupported quantity. It never creates an assumed exit fill. Unresolved
  positions block continuation and readiness. Daily loss halts new entries for
  the day; a drawdown halt persists until a new reviewed evaluation. Gaps and
  unavailable liquidity can still exceed a stop or a drawdown threshold.

Future live alerts would need to consume these same account decisions and
reconcile actual holdings/fills. This change deliberately does not connect the
paper account to the live alert sender or to a broker order API.

## Review criteria

The first possible review requires both 60 complete eligible sessions and 100
closed, funded, quote-validated candidate trades in the primary scenario.
Review looks are fixed at 60, 90 and 120 eligible sessions. Between those looks,
statistical results remain frozen at the latest review sample. Reaching 100
trades between review looks does not create an extra significance test.

Checks require an intact calendar/history, complete quote coverage, reconciled
cash, no unresolved positions, approved dollar risk limits, positive net
expectancy versus untraded cash and improvement over the original-P1A control.
Both confidence-interval lower bounds must be positive. Intervals use SciPy's
tested bootstrap over five-session nonoverlapping blocks. The confidence level
is adjusted for two comparisons and three planned looks (approximately 99.17%
per interval, targeting 95% familywise coverage). Remaining days wait for the
next review rather than being selected for profit. Serial-dependence and regime
assumptions still limit these approximate intervals.

Predeclared price/delay stress accounts must remain profitable; the
missed-first-entry stress must be nonnegative, allowing zero exposure when the
only entry is missed. Risk bounds must be respected; an ordinary daily-loss
halt is logged, while a drawdown halt prevents promotion. Checkpoint success
means ready for a human review, never automatic activation. If the fixed review
schedule ends without enough observations or convincing evidence, a new
prospective plan is required. Software cannot manufacture the future sample.

## Commands and reports

```
pip install -r requirements-quotes.txt
python -m apex_chronos.quote_collector status
python -m apex_chronos.quote_collector collect
```

Set `TRADIER_API_KEY` and `CHRONOS_QUOTE_ARCHIVE_KEY` through a secure environment
or GitHub repository secrets, never in chat or committed files. `APEX_GIT_PUSH=1`
is the existing explicit publishing switch used by the independent workflow.

Quote-only archives and reports use `state/quote_observation_archive/` and
`ledger/quote_observations/`. An activated evaluation uses
`state/quote_trial_archive/` and `ledger/quote_trials/TRIAL_ID/`. `STATUS.json`
contains counts, next review point, uncertainty intervals and every unmet gate.
The existing daily shadow JSON gains a separate `cash_quote_trial` aggregate
block after the existing Discord post. Failure of this attachment leaves all
other reports and live alerts unchanged.

The optional manual CI connection probe captures real provider responses only
into encrypted artifacts. It never funds a paper position or adds trial evidence.
Synthetic tests and retrospective snapshots are clearly excluded.

Validation: the full unit suite, original backtest parity verification and cash
shadow verifier continue to run in CI. Additional tests cover quote-side ages,
units, capacity, encryption, partial exits, settlement, risk, delays, restart,
history tampering, sample thresholds, statistical losses and alert preservation.

Sources: [Tradier market data](https://docs.tradier.com/brokerage-api/market-data),
[Tradier quotes](https://docs.tradier.com/market-data/get-quotes),
[Alpaca feed differences](https://docs.alpaca.markets/us/docs/historical-option-data),
[Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/),
[SciPy bootstrap](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.stats.bootstrap.html),
[selection bias and repeated testing](https://davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
