# ADR 0004: contention, retry and response-loss recovery

Date: 2026-10-23. Status: accepted and covered by integration tests.

## Bounded retry

`submit_purchase` retries only PostgreSQL DeadlockDetected (40P01) and
SerializationFailure (40001). There are at most **three total attempts**, not three
additional retries. Each attempt calls the same complete admission transaction with
the unchanged principal, command and request key. The previous transaction must
have exited and the connection must be open and IDLE before another attempt starts.

Backoff uses jitter of 10–50 ms after the first failure and 20–100 ms after the second.
No transaction or database lock is held during this backoff. Exhaustion raises
`RetrySameIdentity(reason='transaction_retries_exhausted')` with the last database
error as its cause. Failed attempts leave no retained business rejection or partial
financial effect. The wrapper never closes or replaces the caller's connection.

Lock timeout (55P03) and query cancellation/statement timeout (57014) still raise
retryable `database_contention` after rollback, without an internal retry loop.
Unexpected integrity faults propagate unchanged. Business 422 results, identity
conflicts, authorization failures and non-NORMAL workspace mode are not retried.

This follows PostgreSQL's requirement to retry the complete decision-making
transaction, rather than just its final statement. The normal isolation level
remains READ COMMITTED. SQLSTATE 40001 injection tests exercise the retry boundary;
they do not claim that this path naturally uses Serializable isolation or triggers SSI.

## Connection loss and response loss

A returned AdmissionResult means the transaction context received a successful
COMMIT response. If the connection fails, especially during COMMIT, the service
propagates the database/connection exception without assuming rollback, reconnecting,
or inventing an operation ID. The caller opens a new connection and resubmits the
same command and identities. The existing identity constraints and replay path then
resolve whether the operation was committed. A new transport key may link to the
same business ID and bytes, but is never a new financial decision.

The test harness distinguishes three cases:

1. A worker has received COMMIT acknowledgement, then is killed before it sends a
   business reply. An independent observer sees the stored result; the client has
   no reply and recovers it with the same identities.
2. A loopback proxy receives the client's actual COMMIT frame but disconnects before
   forwarding it. PostgreSQL rolls back when the connection ends. The caller only
   sees connection loss; a resubmission creates one successful effect.
3. The proxy forwards COMMIT, observes PostgreSQL's actual COMMIT CommandComplete,
   then withholds that acknowledgement and disconnects. The caller again sees
   connection loss; a resubmission replays the committed effect.

Cases 2 and 3 intentionally produce similar client-visible uncertainty and different
actual database outcomes. Tests inspect protocol events and independent committed
state as observers; that knowledge is not made available to the application caller.
This is test infrastructure, not a production proxy, HTTP server or connection pool.

## Deterministic contention evidence

Prepare inputs before starting workers. Use runtime credentials on separate
connections and observe PostgreSQL blocking relations to release controlled blockers.
Fixed sleeps, timeouts or uniqueness errors do not prove the expected sequence.
Two different purchases of 80 against 100 must produce one accepted and one retained
insufficient-funds result, customer 20 and one purchase journal. Competing identity
claims must see the winner through a fresh READ COMMITTED statement and either replay
or reject changed bytes without a new effect or orphan request key.

A deliberately constructed PostgreSQL deadlock verifies a real 40P01 path. Test-only
SQLSTATE injection uses nontransactional attempt observations to demonstrate that
retries start new transactions and re-run claims/posting rather than just the final
statement. Cancelled/timed-out calls must release transaction locks and claims.
Independent fold and literal expected accounts remain the financial oracles.

Process and wire-proxy tests exercise these schedules against PostgreSQL.

## Payout dispatch without a distributed transaction

The ledger database and the payment provider cannot share an atomic commit. Payout
dispatch follows the approach in Pat Helland, *Life beyond Distributed Transactions:
an Apostate's Opinion* (CIDR 2007; revised in ACM Queue 14(5), 2016). Each side
commits only its own state. They agree through messages that may be retried, and the
receiver keeps a durable memory of which messages it has already applied.

| Helland's idea | LedgerOps mechanism |
|---|---|
| An entity is the unit of serializable state; work across entities is not atomic | The payout row (workspace + operation) is one entity; the provider's payment record is another. No transaction spans both |
| Messages can be delivered more than once, so processing must be idempotent | The provider key `v1.<namespace>.<workspace>.<operation>` is derived from the business identity at admission, stored with `UNIQUE (provider_key)`, and reused on every PUT, LOOKUP and CLOSE. The provider stores the payment and its result under that key |
| The sender records that it is about to send before sending | `claim_payout` commits a MAY_HAVE_SENT attempt and knowledge UNKNOWN **before** the network call. A crash after this commit is treated as "the payment may exist" |
| Uncertainty is resolved by asking, not assuming | Recovery starts with LOOKUP using the same key. A timeout, HTTP 500 or NOT_FOUND before the deadline never means "not paid". After the deadline the worker asks the provider to close the payment if it is absent |
| Tentative work is later confirmed or cancelled | The 62 hold is the tentative state. It is consumed only on confirmed success and released only on confirmed failure or a confirmed close without payment |

The rejected alternatives make the reason for the order clear:

- Sending first and recording afterwards loses the fact that a payment may exist if
  the process dies in between.
- Generating a fresh key per attempt turns a retry into a second payment.
- Releasing the hold after a timeout pays out money that may already have left.

The persistent key is what lets a restarted worker ask a question it can safely
repeat. Without it, "did my last message arrive?" has no safe answer.

Evidence: `tests/integration/test_payout_worker_process.py` kills the worker after
the provider pays and checks for one provider effect. `test_payout_dispatch.py` and
`test_payout_resolution_faults.py` cover lost responses and late completions.

Helland's model also lets entities be repartitioned, which LedgerOps does not need on
one PostgreSQL. Only the idempotent-message and tentative-operation parts are used.

## Limits

These bounded experiments cover purchase retries and response-loss recovery at the
listed fault locations. Automatic reconnection and durable client command storage
are outside that retry boundary. HTTP authentication, reversals, maintenance and
external payout recovery have their own implementations and integration tests.
The runtime remains a trusted credential, and database-owner access can bypass application rules.

Primary semantics: [PostgreSQL whole-transaction retry guidance](https://www.postgresql.org/docs/18/mvcc-serialization-failure-handling.html)
and [frontend/backend message formats](https://www.postgresql.org/docs/18/protocol-message-formats.html).
The executable protocol tests, rather than a presumed exception outcome, establish
which COMMIT frame reached the pinned PostgreSQL server.
