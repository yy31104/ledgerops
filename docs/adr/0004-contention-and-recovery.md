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

## Limits

These bounded experiments establish the tested schedules and fault locations, not
exhaustive correctness for every network, scheduler or database failure. They do not
implement automatic reconnection, durable client command storage, production HTTP
authentication, end-to-end external payment delivery, reversal or maintenance APIs.
The runtime remains a trusted credential, and database-owner access can bypass application rules.

Primary semantics: [PostgreSQL whole-transaction retry guidance](https://www.postgresql.org/docs/18/mvcc-serialization-failure-handling.html)
and [frontend/backend message formats](https://www.postgresql.org/docs/18/protocol-message-formats.html).
The executable protocol tests, rather than a presumed exception outcome, establish
which COMMIT frame reached the pinned PostgreSQL server.
