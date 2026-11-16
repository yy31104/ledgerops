# The provider paid. Then the worker died.

A worker can fail after a provider commits a payment and before the ledger records
the response. Retrying the payment with a new key can pay twice. Releasing the hold
can let the merchant spend money that has already left. LedgerOps treats this gap
as an unresolved fact to investigate.

## One payment, two durable histories

Suppose a merchant has 100 units and requests a payout of 80. Admission reserves
80 in a hold, leaving 20 available. The ledger stores a stable provider key with
the payout. That key belongs to the business operation, rather than a worker process.

A dispatch worker claims the operation with a lease. Before making a network call,
it commits the knowledge state MAY_HAVE_SENT. The provider might never receive the
request, or might commit it while the response is lost. The ledger must therefore
keep both possibilities open. No database transaction stays open across that call.

Now the provider commits the payment. The demo pauses its response at exactly that
point, kills the worker with SIGKILL, and restarts it. At this moment the external
payment exists, while the local hold remains unresolved. A process restart cannot
turn that uncertainty into a failure or free the held funds.

## Recovery asks before it sends

The replacement worker waits for the old claim's real lease to expire. It queries
the provider using LOOKUP and the same persistent key. An authoritative SUCCEEDED
observation allows the ledger to consume the hold and create the outgoing journal.
The resolution transaction applies that outcome once. Replayed success evidence
cannot create another settlement.

If the provider instead reports an authoritative failure or a closed-absent key,
the ledger can release the hold. A timeout, an empty response, or a dead process
alone cannot justify release. The lease prevents concurrent ownership; it does
not prove whether the provider executed a payment.

## What the demo proves

Run `make demo-payout-crash`. The screen identifies the provider's durable commit,
the killed process, lease expiry, LOOKUP recovery and final independent checks.
It inspects provider execution/effect counts and ledger settlement/journal counts,
then replays the observation and runs the invariant verifier. The GIF in the
homepage records this real local run; it is not a sequence of illustrative slides.

The result is one provider payment and one ledger settlement for this injected
failure. This guarantee depends on the provider honoring stable-key idempotency,
authoritative lookup/closure semantics and the ledger's transactional constraints.
It does not make arbitrary third-party providers transactional, or prove every
network failure possible in production.

## How to learn the boundary

Read the [dispatch and recovery rules](../docs/PAYOUTS.md#dispatch-and-recovery),
then follow admission, dispatch and resolution in the implementation. Predict what
would happen if MAY_HAVE_SENT were committed after the network call, or if a retry
used a fresh provider key. The process-level fault tests turn those predictions
into observable failures. Reconciliation supplies another independent route for
finding disagreements between external evidence and local state.

The useful lesson is to preserve uncertainty until durable evidence resolves it.
A retry loop alone is not a payment-recovery protocol.
