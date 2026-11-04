# Payout admission, retained instructions and dispatch

The Python interfaces introduced in R3-PR02/03 reserve funds, accept authenticated
provider observations and settle each payout locally at most once. The HTTP service
and the bounded worker are separate entry points. The provider is a synthetic local
protocol; its behavior does not establish a connection to a bank.

## Amounts and admission receipts

After a customer buys 80 from an opening balance of 100, the customer has 20, the
merchant has 80 and clearing assets remain 100. A payout of principal 60 with a fixed
fee of 2 has a gross amount of 62. Successful admission creates the payout, a HELD
reservation, an immutable initial event and a READY dispatch intent in one PostgreSQL
transaction. It increases the merchant's reserved amount from 0 to 62 and increments
the balance version. It does not create an outgoing journal. The merchant then has
booked 80 / reserved 62 / available 18; clearing assets are still 100.

`submit_payout(conn, principal, command, request_key, policy=..., clock=...)` returns
202 `accepted_for_processing` after COMMIT. It requires an idle autocommit connection
and a `Principal` supplied by a trusted authentication boundary. Requests use
`PayoutCommand` and the `payout:v1` request-key route. Canonical fields are
operation_id, merchant_account_id, beneficiary_id, provider_namespace, principal_minor,
fee_minor, currency and contract_version. The workspace comes from trusted scope;
amounts are canonical decimal integer strings in minor units.

`TrustedPayoutPolicy` supplies the workspace, provider namespace, fixed fee, dispatch
window and beneficiary-to-merchant-owner binding. Do not construct this policy
directly from an untrusted HTTP body. Only the original merchant principal may admit
or read the payout; knowing a UUID grants no role. New operations check enabled
accounts and available funds. Insufficient funds produce a retained 422 without a
hold. Reusing an identity with different content returns 409; authorization failures
return 404. Neither creates a new retained result.

Admission locks merchant and clearing balance rows in UUID order. After obtaining
the clearing lock, it subtracts clearing reserved funds and all active HELD gross
amounts in the workspace from clearing's booked balance before accepting another
payout. Unknown or blocked payouts remain committed capacity. Two merchants cannot
both reserve 62 against clearing capacity of 100; the second receives a retained
422 `LIMIT_EXCEEDED` without a hold.

Only new admission samples the clock, fixes dispatch_deadline and saves the exact
provider request bytes and digest. This server time is absent from the client's
canonical request. Replay returns the original 202, body and deadline even if the
current quote, window or later payment state has changed.
`get_payout_state(conn, principal, payout_id)` reads current progress separately; it
does not rewrite the admission receipt. Adding funds today does not change an earlier
retained refusal.

## Dispatch and recovery

`ledgerops.db.payout_dispatch` exposes `due_payouts`, `claim_payout`, `finish_claim`
and `dispatch_once`. Scans are batch-bounded and take no row locks. After taking the
shared workspace barrier, a claim rereads the mode, locks operation -> payout ->
intent, records a MAY_HAVE_SENT attempt, sets knowledge to UNKNOWN and advances the
next due time. **It commits before making a network call.** A claim that does not
commit sends nothing. A process death between commit and send is still treated as
an unknown outcome.

The first call is PUT. Once a lost process's old claim expires, recovery starts with
LOOKUP. Before the dispatch deadline, a missing result or failed observation transport
leads to bounded resending with the same key and original bytes. After the deadline,
it requests close-if-absent. The deadline is checked again immediately before sending,
so a claim paused across the deadline cannot initiate a new PUT. An in-flight PUT can
still succeed before closure; the deadline is not cancellation. ACCEPTED and terminal
responses continue through observation processing. HTTP 200 or transport success alone
cannot change funds.

Each `dispatch_once` makes at most one network call. Early rounds permit at most five
shorter backoffs; later calls enter a slower observation cadence with bounded delay
and jitter. Candidates, attempts and responses have explicit size limits. Exhausted
attempts, timeouts, HTTP 500, NOT_FOUND and workspace freezing cannot release the 62.
Database retries cover only transactions known to have rolled back with 40P01/40001;
network calls stay outside the database retry body.

A late completion of an old attempt records only its own facts; it cannot overwrite
a newer claim's scheduling state. FROZEN/RESTORE blocks new ledger writes and retained
observations, without acknowledging durable receipt. Observe again after recovery.
Freezing does not stop external requests already sent. PUT preflights settlement
capacity. LOOKUP/CLOSE creates no new payment and can gather evidence despite a local
projection blocker. A bounded preflight cannot guarantee that later concurrency will
leave cumulative numeric or version capacity available.

`ProviderHTTPClient` accepts explicit 127.0.0.1 addresses only, with no automatic
retries, redirects or environment proxy. `ProviderTrust` supplies the namespace and
at most eight verification keys. Request bytes, key, merchant, beneficiary, amount,
currency, protocol version, issuer, event ID and sequence must all bind before an
observation is authenticated. NOT_FOUND proves only temporary absence at one lookup.
Responses, lookups, closure, callbacks and history recovery enter the same authenticated
decision path. HTTP status is not payment authority: evidence must be checked even
when transport status is abnormal, and invalid evidence cannot move money.

## Roles, migrations and rebuilds

Before applying 006, an administrator creates the worker login separately and sets
its password privately:

```sql
CREATE ROLE ledger_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_worker
```

Do not grant app, migrator or maintenance role membership. The worker may read
instructions and claim or complete transport attempts. At migration 006 it cannot
create business operations, change balances, write entries or modify holds. Migration
007 adds the journal, balance and hold-state privileges needed for terminal resolution.
It still cannot create business operations or rewrite history. Trusted Python decision
code must exercise these privileges; the database does not verify HMAC on its behalf.
The narrow operation UPDATE-column grant permits `SELECT ... FOR UPDATE`; existing
immutability triggers still prevent changes to retained operations.

Migration connections do not create roles; migrations 001-005 remain unchanged. An
upgrade explicitly fails if an old database has nonzero reserved amounts without
hold provenance.

Export v5 includes payouts, holds, events, intents and attempts. V6 adds observations,
receipts, quarantine, incidents and resolution relations. Older v1-v5 packages retain
their original reading capabilities. Check A folds journal debit/credit totals and
immutable hold events into reserved amounts; check B compares local admission effects
with retained requests. A legitimate hold of 62 does not prevent projection repair:
freeze and drain writers -> preview -> recompute inside an exclusive transaction,
record complete before/after values and epoch -> verify -> resume. Holds and events
remain unchanged. See [maintenance](MAINTENANCE.md) and
[export verification](EXPORT-AND-VERIFY.md).

## Scope of the result

202 is admission, not payment success; query state for subsequent processing. An
offline export without trusted secret keys cannot prove HMAC authenticity. V6 reports
distinguish internal consistency, pending work, contradictions and unverified external
authenticity. Agreement between A and B does not prove the original request, quote or
beneficiary configuration is true. The simulator supplies permanent keys and retained
records of closure; a real provider may not. Real funds and production identity
configuration require their corresponding environments. The bounded worker and crash
matrix follow the PR04 rules below; recorded acceptance belongs to its development log.

## Authenticated observations and one-time resolution (007)

`ledgerops.db.payout_resolution.receive_observation(conn, workspace, payout_id, raw,
trust=..., channel=..., transport_attempt_id=...)` accepts at most 64 KiB of raw response
and returns a structured decision. Signing binds the original payload, key ID and
protocol domain. Exact signed payload/signature bytes and the canonical envelope are
retained. Outer JSON formatting changes do not create a new event; receiving the same
logical event over another channel does not change its signed bytes.

- SUCCEEDED: retain the observation, Dr merchant 62 / Cr clearing 62, CONSUMED event,
  balances and DONE in one transaction. Final merchant values are 18 / 0 / 18 and
  clearing is 38.
- FAILED/CLOSED_ABSENT: retain evidence and RELEASED together. The merchant returns
  to 80 / 0 / 80 with no outgoing journal.
- NOT_FOUND/ACCEPTED: preserve HELD. A failed lookup after ACCEPTED remains observation;
  it cannot revert to an unknown initial dispatch.
- A compatible repeated terminal event or old nonterminal event retains required
  receipts without moving funds again, rewriting the original 202 or replacing the
  terminal sequence.
- Authenticated event-ID/sequence reuse, incompatible terminal results, or a higher
  ACCEPTED sequence after a terminal event retain both evidence records and an
  incident, then stop automatic processing for that payout. An older contradictory
  terminal event cannot be ignored either.

Unauthenticated or incorrectly bound messages enter bounded quarantine, without a
financial incident. Each payout permits at most 100 distinct raw quarantine messages.
After that, additional diagnostic input is refused while a valid terminal result can
still be processed. Authenticated observations are limited to 1,000. At capacity, the
system explicitly retains a summary and stops automatic decisions rather than claiming
a complete evidence set. Repeated delivery of the same event does not consume capacity.

Lock order is shared workspace barrier -> provider namespace lock -> operation/payout/
intent -> hold -> UUID-sorted balances. The namespace lock serializes comparison of
events from one provider, at an explicit throughput cost. A cross-workspace collision
creates an incident only in the current workspace; linked observations stop subsequent
processing of the other payout. It does not bypass the other workspace's freeze
barrier to change its rows. That workspace's export sees local references and conflict
summaries, without the other workspace's request or identities. Already in-flight
external calls are not recalled.

Freezing fixes a workspace's financial writes. Global provider-collision evidence
received later by another workspace may still appear in linked diagnostics. Each
export reads that evidence in its own single database snapshot; the workspace barrier
does not freeze global evidence.

Before resolution, the system checks source entries against current debit/credit
projections, active-hold totals and numeric/version capacity. Disabling a merchant
later does not invalidate an external payment that already occurred. A known
projection/capacity obstacle first rolls back the financial attempt, then separately
retains evidence and a LOCAL_BLOCKER. The original fund state is preserved and
automatic processing stops. Arbitrary SQL/COMMIT errors still roll back and propagate;
they cannot be converted into business refusals.

Repairing a projection does not automatically clear an existing blocker; controlled
reprocessing requires the R5 approval path. This differs from the first normal receipt
of success evidence after an active-hold repair, which may settle normally.
FROZEN/RESTORE does not acknowledge durable observation receipt: callers must redeliver
or query after recovery. A v6 `healthy` result does not mean all payouts were paid;
read the pending, incident, blocker and resolved counts in `external_evidence` as well.

## Bounded worker process (R3-PR04)

The installed package provides `python -m ledgerops.worker`, a dedicated background
process. It uses a separate `ledger_worker` login. Database owners, migration roles
and privileged roles are rejected at startup. Configuration comes from the environment;
the following entries describe variables, **not reusable production passwords**:

| Variable | Meaning |
|---|---|
| `LEDGEROPS_WORKER_DSN` | Dedicated worker PostgreSQL connection; inject its secret from the deployment environment |
| `LEDGEROPS_PROVIDER_URL` | 127.0.0.1 HTTP address for this simulator adapter |
| `LEDGEROPS_PROVIDER_TOKEN` | Provider command-channel credential |
| `LEDGEROPS_PROVIDER_NAMESPACE` | Persisted provider UUID; do not regenerate it at restart |
| `LEDGEROPS_PROVIDER_KEYS_FILE` | Regular JSON file owned by the current process user, readable only by that owner; maps key IDs to hexadecimal HMAC keys, at most eight |
| `LEDGEROPS_RESTORE_MODE=false` | Explicitly releases the separate restore switch; omission defaults to restore mode and refuses startup |
| `LEDGEROPS_DISPATCH_ENABLED=true` | Explicitly enables dispatch; omission refuses startup |

Bounds: batch size defaults to 20 (1-100), polling to 1,000 ms (50-60,000), lease to
30 s (1-300), and the complete HTTP attempt to 5,000 ms (1-30,000). Configure these with
`LEDGEROPS_WORKER_BATCH_SIZE`, `LEDGEROPS_WORKER_POLL_MS`,
`LEDGEROPS_WORKER_LEASE_SECONDS` and `LEDGEROPS_PROVIDER_TIMEOUT_MS` respectively.
The lease must cover the provider timeout plus 5,000 ms or configuration is rejected.
Stable identity and unique resolution still provide duplicate protection; lease expiry
is not payment cancellation. Do not lower deployment values merely to observe faster.

`--once` scans one batch and exits. `--max-calls N` exits after completing N attempts;
it waits when nothing is due and does not mean "wait at most N seconds". Ctrl-C or
SIGTERM stops new claims, then closes the current database/HTTP chain within its
existing bounds. SIGKILL can leave an unknown outcome; restart recovers from retained
claims using the same key.

Process connections use 10 s statement, 5 s lock, 30 s transaction and 15 s
idle-in-transaction guards on PostgreSQL 18. These are independent limits, not a
promise that the whole process exits within five seconds. Restoring an old NORMAL
workspace cannot bypass the separate restore/dispatch switches; do not enable them
before the R5 restore procedure completes. Database freezing cannot recall network
requests already sent.

Logs are JSON events for readiness, idle state, attempt/result categories, pause
reasons, errors and exit totals. They omit raw exception text, DSNs, passwords,
signing keys and payment instructions. `worker_ready` is emitted only after real
connection and capability checks pass. Trusted-data invariant or structural failures
exit with code 2 for investigation rather than skipping bad data and claiming global
health. Recoverable database connection failures reconnect on later polls. Completed
attempt counts are not payment counts.

This process is not an adapter for a production payment provider. Real-bank terminal
semantics, idempotency retention, authority of negative results, TLS and credential
rotation each require integration and verification. Changing a URL cannot transfer
the simulator's closure guarantees to a real bank.
