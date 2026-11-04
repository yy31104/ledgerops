# Full reversal by the original merchant

A reversal is a new business operation and journal. An original purchase records
Dr customer 80 / Cr merchant 80; its reversal records Dr merchant 80 / Cr customer 80.
The original purchase remains successful, and its entries, header and response stay
unchanged. This path reverses a correct internal platform purchase in full. It does
not implement channel refunds, partial refunds, unilateral customer debits against a
merchant, or operator approval.

## Python entry point

The interface matches purchasing: an idle autocommit psycopg connection, trusted
Principal, typed command and request key. Amount, currency and both accounts are
verified against the successful original purchase; callers cannot override them.

```python
from ledgerops.db.admission import Principal, submit_reversal
from ledgerops.domain import ActorId, ClientId, ReversalCommand, RequestKey, WorkspaceId

# workspace_uuid / merchant_actor_uuid / client_uuid come from a trusted boundary.
# A UUID is not authentication. original_purchase_uuid identifies a committed success.
workspace = WorkspaceId.parse(workspace_uuid)
principal = Principal(workspace, ClientId.parse(client_uuid), ActorId.parse(merchant_actor_uuid))
command = ReversalCommand.from_payload(workspace, {
    "operation_id": reversal_uuid,
    "original_operation_id": original_purchase_uuid,
    "reason": "customer_refund",
})
key = RequestKey(workspace, principal.client_id, "refund-transport-key", "reversal:v1")
result = submit_reversal(conn, principal, command, key)
```

Only the original merchant account's owner may initiate or read a reversal. The
original customer, another merchant or someone who merely knows the purchase UUID
gains no permission from that knowledge. Workspace, client and request route must
match. At this Python boundary, the caller must authenticate Principal rather than
trust an actor field from the request. The service adapter is described in
[HTTP service](HTTP-SERVICE.md).

The reason enters canonical bytes unchanged. It must be nonempty valid UTF-8,
contain no NUL and fit within 256 bytes. A new operation ID identifies a new business
decision; a request key identifies transport. Purchases and reversals share the
workspace's operation-ID namespace. Request keys are scoped by workspace/client/route/key.

## Disabled original customer

A new reversal requires both original customer and merchant accounts to be enabled.
If the original customer is disabled, it returns generic 404 and rolls back without
retaining a request, operation or reversal attempt. This differs from a retained 422
for insufficient funds. After reenabling the account, the original identity can still
be submitted. Authorized historical replay does not recheck later changes to enabled
state. The reversal admission tests explicitly cover this behavior.

## Results and retries

| Situation | Result |
|---|---|
| Eligible original purchase; merchant has sufficient available funds | Retain 201, a reversal journal and the success relation |
| Insufficient available funds | Retain 422 `INSUFFICIENT_AVAILABLE`; no journal or success relation |
| Cumulative totals or account versions reach a bound | Retain 422 `LIMIT_EXCEEDED`; no financial effect |
| A successful reversal already exists; a new operation ID tries again | Retain 422 `ALREADY_REVERSED`; no second debit |
| Same identity and content are resubmitted | Return the original success or refusal, including its diagnostic request_id |
| An authorized request key/operation ID is reused with different content | Unretained 409 identity conflict; provisional claims roll back |
| Original purchase is absent, ineligible or unauthorized | Generic 404; no new claims |
| Retained request, original entries or result are corrupt | Internal invariant error and rollback, not a fabricated business refusal |

The 201 body contains the new operation_id, original_operation_id, journal_id,
string amount_minor, currency, admission, `effect="purchase_reversed"` and integer
contract_version=1. It does not contain current balances. Identity-conflict handling
also checks retained operation ownership first. Supplying one authorized object
cannot expose another object's result.

With merchant booked 140, reserved 100 and available 40, reversal of 80 is refused.
After adding 40, the old operation ID still returns its original 422; a new operation
ID can be reconsidered and succeed. Multiple rejected attempts remain retained, with
at most one success relation. Reserved funds stay unchanged. After success the
merchant has booked 100 / reserved 100 and the customer has 100. Synthetic funding
and reservation setup in tests are not new public interfaces.

40P01/40001 can retry at most three complete transactions. Timeouts, cancellation,
arbitrary integrity errors and connection loss are not automatically retried. A
COMMIT disconnect can leave the outcome unknown. Reconnect and reuse the original
operation ID, content and request key rather than guess with a new business identity.
A new request key with the same operation ID can also retrieve the result; the
original key additionally checks the original transport association.

## Locks, uniqueness and independent verification

Purchases and reversals share setup, the workspace shared barrier, mode checks,
identity claims, replay and bounded retries. New reversal order is request key -> its
own operation ID -> original purchase row FOR UPDATE -> balance rows sorted by
account UUID. Only a committed successful purchase qualifies as the original.
Maintenance first takes the exclusive workspace barrier, without taking balance
locks early.

When two new reversal IDs target one purchase, the second waits for the original
purchase lock. After the first commits, the second sees the success relation and
retains ALREADY_REVERSED. The success relation's primary key is the final database
guard. An unexpected 23505 propagates instead of being treated as success or ordinary
replay. Contention on the same request key uses the purchase admission unique-claim waiting rules.

`reversal_attempt` retains each eligible attempt; `purchase_reversal_effect` contains
only successful relations. Composite foreign keys bind workspace, kind and original
operation. Deferred constraints require a complete journal and success relation for
accepted reversals, and no financial effect for refused ones. New history tables
reject updates and deletes; committed journals still reject later appends. Python
and the independent verifier check account ownership, canonical requests and actual
entries. Balanced debits and credits alone cannot establish those properties.

Export v4 includes all attempts and success relations. The verifier reparses original
purchase and reversal requests using only the standard library, checking reversed
accounts, amounts, responses and relations. A refund consistently posted to the wrong
owner in both entries and balances can pass A but fails B. Frozen v1/v2/v3 exports
remain readable and verifiable.

A reversed ledger can still use [maintenance](MAINTENANCE.md). Rebuild requires B
and valid reservation provenance; formats predating hold support require zero
reserved amounts. Resume requires structure/A and retains B/reservation diagnostics.
Full reversal handles an originally correct purchase; it is not a general repair
for corrupt original accounting or every historical business error. Finite tests do
not exhaust all interleavings or external failure schedules.
