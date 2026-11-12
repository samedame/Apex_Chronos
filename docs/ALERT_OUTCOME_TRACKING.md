# Immutable alert evidence and outcome reconciliation

The final-bar ledger (`ledger/trades.csv`) answers what the model would have
done on the final inputs. It is recomputed at EOD. It is not an immutable record
of what was alerted, nor a broker fill ledger. For the saved October 15–November
11 data, there are 114 final-model trades, 107 entry keys marked posted, 92 keys
in both sets, 15 posted-only keys, and 22 final-model-only keys. The 124 legacy
entry records include 17 notices marked not posted/actionable. Counting all 124
as live alerts conflates those cohorts.

This change preserves the existing final-bar ledger, EOD message, ENTRY/TP1/EXIT
formatting, selection, delivery retries/deduplication and default-disabled cash
controls. The new reports do not send extra messages or make broker requests.
They do not change the frozen quote trial or count as quote-validated trades.

## Stored evidence

`state/alert_journal/YYYY-MM-DD/<sha256>.json.gz` contains separate observations:

- Session context and strategy source hashes, plus selected nonsecret settings.
- As-consumed OHLCV, VIX and feature arrays, including earlier-bar revisions,
  degraded-minute flags and a parent snapshot reference. Initial snapshots are
  complete prefixes; subsequent snapshots contain indexed changes and array
  lengths. Missing tickers are identified by the current `features` key set.
- Engine ENTRY/TP1/EXIT/SKIP events and their original model entry references.
- The exact text, digest and position identity of each delivery intent, written
  before calling the existing delivery function; the returned acknowledgment
  is written immediately afterward and before updating the legacy sent file.
- Existing-key suppression records, not new delivery acknowledgments.
- Provenance-labeled legacy sent snapshots and every final-model replay
  revision. An EOD snapshot includes its exact input/context data when available.

Files are compressed canonical JSON. The file name hashes the uncompressed
content; reads verify that hash and schema. Writes fsync and create files
exclusively, without replacing an existing record, including under concurrent
writers. Existing bytes remain unchanged when EOD runs again. The journal uses
ordinary file/Git permissions: this is application-level append-only evidence,
not independently certified WORM storage. A privileged user can delete files;
Git history, retained artifact checkpoints and stored record references provide
additional witnesses. Do not rewrite or garbage-collect journal records as part
of normal EOD processing.

The observer records no webhook URL, token, encryption key, broker balance or
private cash-book contents. Existing model prices and public alert messages are
recorded; actual Tradier raw quotes continue through their separate encrypted
archive. No provider call is added by this feature.

## Cohorts and outcomes

`ledger/alert_outcomes/YYYY-MM-DD.json` is a rebuildable view of the immutable
records. Its important fields are:

| Field | Meaning |
| --- | --- |
| `acknowledged_webhook_entry_keys` | Unique actionable entry keys with a recorded successful return from Discord delivery. |
| `legacy_marked_posted_entry_keys` | Older entry keys known only from saved metadata; not reconstructed delivery receipts. |
| `stale_or_nonactionable_entry_keys` | Stale notices or other nonactionable entry records, separate from posted entries. |
| `posted_keys_matching_final_model` | Posted entry identities also present in the selected final snapshot. |
| `posted_only_entry_keys` | Posted identities the final replay did not select; never deleted from the alert cohort. |
| `final_model_only_entry_keys` | Final-model identities without posted-entry evidence. |
| `communicated_model_roi` | The original model ROI in an acknowledged exit, only with consistent original entry/scaleout evidence. |
| `complete_posted_cohort_mean_model_roi` | Null unless every posted position has recoverable, consistent communicated model outcome evidence. |
| `broker_verified_pnl` | Always null; this observer does not infer fills, actual quantity, costs or account PnL. |

A transport false return or interrupted intent is **unconfirmed**, not proof of
failed delivery. A true return is a webhook acknowledgment, not proof of
recipient action. Dry runs and unconfigured destinations are excluded from the
acknowledged live cohort. Duplicate acknowledged attempts are flagged rather
than assumed to represent one broker position.

The original entry plan is never repriced from final bars. If a restarted live
engine reconstructs a different entry basis, contract/plan or economic code
version, its exit cannot silently close the original model position. It is
reported as `unresolved_entry_plan_revision`. Missing exits, missing origin
references, inconsistent exit basis and unacknowledged scaleouts also stay
unresolved. The final-model trade remains available alongside that status as a
separate diagnostic. No counterfactual exit is presented as a communicated exit
or actual fill.

Legacy sent files lack the original text, prices and inputs. Migration preserves
their metadata and marks original outcomes unavailable, even when a matching
final-model trade exists. It cannot recover the missing historical evidence.
Likewise, unavailable final data are null/unavailable, not a zero-trade day.

## Offline reconstruction

Copy the desired saved `state/` and `ledger/` into a research directory, then:

```sh
python -m apex_chronos.alert_journal --state-dir /research/state --ledger-dir /research/ledger
# Restrict to one session:
python -m apex_chronos.alert_journal --state-dir /research/state --ledger-dir /research/ledger --date 2026-11-11
```

This command writes journal snapshots and derived reports only. It never sends
alerts, contacts a market/broker API, pushes Git state or modifies `trades.csv`
or the old `sent-*.json` files. Importing unchanged legacy metadata is idempotent.
Re-running a final snapshot records another observation instead of overwriting
the earlier snapshot. Report JSON is intentionally a replaceable projection;
the underlying evidence and its record IDs remain immutable.

In the live workflow the journal travels with the already committed `state/`.
The EOD report travels with `ledger/`. Both workflows also upload a checkpoint
with `if: always()` using the platform's artifact retention. The session handoff
occurs before checkpoint upload. This offers recovery if a later Git push fails;
it does not guarantee persistence if an ephemeral runner is destroyed before a
push or artifact upload. No synchronous Git/network barrier is inserted before
alerts, and the existing retry behavior is not changed in this PR.

Observation failures print a stable `alert tracking incomplete` diagnostic and
do not suppress existing alerts. Corrupted records fail reconciliation, never
produce a successful partial report. A previous derived report can remain after
a failed refresh. Consumers must first check `YYYY-MM-DD.status.json` for
`projection_ready`, matching snapshot ID and recency; `refreshing` or
`refresh_failed` must not be treated as a current successful projection. Storage
failure may prevent even the status update, so run/checkpoint logs still matter.

## Verification

Unit tests cover exclusive writes/concurrency, tamper detection, failed storage,
interrupted transport, persistence of receipts before sent-file failures,
stale/dry-run separation, EOD revisions, restarts, missing scaleouts, revised
entry plans, historical imports and input revisions. Existing frozen formatter
bytes remain pinned. The additional CI step:

```sh
python -m verification.verify_alert_tracking --data-dir verification/fixtures.tar.gz
```

uses a synthetic delivery recorder with network requests blocked in temporary
directories. It replays all 21 original sessions/110 trades, restarts delivery
once per session, verifies no extra delivery and exact original model outcomes,
then removes a final-model trade to verify the posted-only outcome survives.
It creates no real alert, broker fill or prospective trial evidence. The
original backtest oracle, fault/restart/fuzz checks and strict cash catalog/
settlement replay remain separate required CI steps.
