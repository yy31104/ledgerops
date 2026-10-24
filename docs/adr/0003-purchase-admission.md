# ADR 0003: Python purchase admission, R1-PR02

Date: 2026-10-23. Status: implemented locally; verification recorded in the PR02 evidence directory. Yi's “continue”
authorizes the next slice; the four business defaults remain the accepted packet.
These are implementation choices, not additional claims of user mastery.

## Service boundary and authorization

`submit_purchase(conn, principal, command, request_key)` owns a whole transaction.
The connection must be autocommit and IDLE; an existing transaction is rejected so
returning a result cannot accidentally precede an outer COMMIT. The service forces
READ COMMITTED / READ WRITE and bounded lock/statement timeouts. It returns only
after psycopg's transaction context acknowledges COMMIT, including deferred checks.

Principal carries typed workspace, client and customer actor IDs from a trusted
caller. There is no token verification or HTTP adapter yet. UUID possession or a
session variable is not authentication. The command and key must match this scope;
the debit account must be a customer liability owned by the principal's actor.
The merchant account ID in the validated command is the chosen payee; resolve its
immutable owner/class from the database, without adding a second wire identity.

Check scope before DB access. Inside the shared workspace barrier, check mode then
customer ownership before reserving identity. On collisions, authorize the customer
in the **stored** canonical command before revealing a conflict or result, even if
the incoming command points to another customer the caller owns. Missing, wrong-
class and unauthorized accounts all raise generic NOT_FOUND (404) with no retained
claim. New purchases also require enabled accounts. A permitted historical replay
still returns its original result when the account is disabled or its balance changes.
Mutable administrative account changes must use the workspace's exclusive barrier;
raw owner SQL can bypass this service contract, as it can bypass all other guards.

## Identity and transaction order

Use the source's exact workspace lock mapping:
`hashtextextended('ledgerops:workspace:' || workspace_uuid::text, 0)` and
`pg_advisory_xact_lock_shared`. Take it before reading mode or writing a claim.
Non-NORMAL mode raises retryable 503 RETRY_SAME_IDENTITY without persisted result.
R2 provides the maintenance freeze interface; tests exercise its SQL counterpart.

Claim request key first with `ON CONFLICT ON CONSTRAINT request_result_pkey`, then
business ID with `ON CONFLICT ON CONSTRAINT operation_pkey`. Each losing INSERT
uses a separate SELECT at READ COMMITTED to see the committed winner. Compare
canonical bytes, not only SHA-256. Conflict errors roll back the whole transaction,
including a provisional new key. Stored request and operation must agree in payload,
status and body before replay; invalid canonical bytes, incomplete results, or a
response/header mismatch raise an internal invariant error, never synthesized success.
JSON type comparisons distinguish booleans from numbers. These checks do not re-audit
historical line ownership or projection correspondence; the independent oracle has
that separate role. Terminal rows remain immutable.

For a new operation, lock balances one at a time in ascending account UUID order.
Read identity/enablement and balances together, check available funds and bounded
results, write two declared journal lines, and apply one relative UPDATE per account.
Finalize both stored results within this same transaction. Opening journals are
explicit privileged fixture setup, not a public funding API.

PR03 supersedes the retry/transport limits below; see [ADR 0004](0004-contention-and-recovery.md).
The following section records the PR02 boundary and its stable outcome shapes.

## Outcomes and error precedence

1. Invalid typed inputs / connection state fail before admission. Mismatched scope
   returns generic NOT_FOUND. Workspace missing returns NOT_FOUND; non-NORMAL mode
   returns retryable 503. Authorization precedes identity disclosure.
2. Same key with changed canonical bytes returns IDEMPOTENCY_CONFLICT (409).
   New key / same business ID with changed bytes returns BUSINESS_ID_CONFLICT (409).
   Both are unretained errors; existing records are unchanged.
3. New purchase with insufficient available funds persists 422 INSUFFICIENT_AVAILABLE.
   Otherwise cumulative-total or projection-version overflow persists 422 LIMIT_EXCEEDED.
   Neither creates a journal or updates balances. Fresh funding does not change the
   old result; a new business decision requires a new operation ID.
4. A successful response follows the source's immutable 201 shape: operation_id,
   admission=accepted, journal_id, amount_minor string, currency=PLN,
   effect=purchase_posted, contract_version=1. It contains no current balance.
5. Rejection body is `{error:{code,message,retryable:false,operation_id,request_id}}`.
   Generate its diagnostic request_id once and retain it; retries return that same
   body. Unretained typed errors expose code/status/retryable to the future adapter.
6. Lock/statement timeout, deadlock or serialization failure raises retryable 503
   after rollback. PR02 does not internally retry transactions; the PR03 retry policy
   will add bounded whole-transaction retries. Other integrity errors propagate.
   Connection loss, especially during COMMIT, remains outcome-unknown to the caller;
   never infer rollback or substitute a new identity. Fault/transport recovery is PR03.

## Verification scope

Drive the actual service with runtime credentials. Check independent entry fold and
original-owner expectations, retained success/rejection replay, new-key binding,
unauthorized collisions, scope separation, balance limits, rollback under real SQL
faults and shared-barrier ordering. No production failpoint parameter is added.
Broader simultaneous-debit and transport-failure tests remain PR03. Keep PR01's
original evidence manifest intact; record PR02 as a separate run.
