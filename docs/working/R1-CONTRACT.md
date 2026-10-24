# R1 working contract — purchase core through PR03

This concise contract applies to the implemented foundation and purchase service. The broader source
blueprints remain proposals for later slices. [ADR 0002](../adr/0002-r1-foundation.md)
records foundation amendments, [ADR 0003](../adr/0003-purchase-admission.md) records the
purchase transaction, [ADR 0004](../adr/0004-contention-and-recovery.md) records retry/
recovery behavior, and [packet 1](../DECISION-PACKET-R1.md) records user choices.

## Data and authority

Synthetic PLN only, integer minor units. Customer/merchant accounts are credit-normal
liabilities; clearing is a debit-normal asset. Account identity/class/normal side
cannot be changed through normal DML. All relation references carry workspace scope.

| Object | Runtime SELECT | Runtime INSERT | Runtime UPDATE | Other authority |
|---|---|---|---|---|
| workspace | Yes | No | No | Future mode changes require maintenance authority. |
| account | Yes | No | No | Privileged setup creates immutable identities; enabled can change under owner. |
| account_balance | Yes | No | debits, credits, reserved, version | Booked is generated; totals are bounded finite integers; booked ≥ reserved. |
| operation | Yes | Yes | admission, http_status, response_body | Exactly one pending→terminal update; no committed pending row. |
| request_result | Yes | Yes | http_status, response_body | Exactly one pending→terminal update; deferred scoped operation FK. |
| journal | Yes | Yes | No | Immutable declaration; one journal per workspace/operation. |
| journal_entry | Yes | Yes | No | Positive bounded entries; one account per journal; final declaration check. |
| schema_migration | No | No | No | Migration owner only; version and checksum. |

DELETE/TRUNCATE, schema/trigger changes, temp tables and role escalation are denied
to runtime. Direct terminal INSERT is permitted by this SQL foundation for fixtures;
Python admission starts pending and validates authorization/business rules.
Accepted results use 201; business rejections use 422. JSON result bodies must be
objects, and both status/body are present together. The Python service implements
purchase/replay; there is no HTTP endpoint or token verifier yet.

Each journal has 2–16 declared lines and a positive total for each side. Both debit
and credit sums must equal that total, with exactly the declared number of lines.
Header/entry insert events trigger deferred checks of final state. A later balanced
pair cannot extend history. SET CONSTRAINTS IMMEDIATE or savepoint use cannot waive
the check. The migration owner remains trusted and can deliberately alter/drop guards.

## What requires independent checks

Database-valid writes can still be wrong: a balance update without journal entries,
a journal with one missing projection update, or a transfer to a different merchant
with matching projection. SQL constraints do not validate request intent.
An ACCEPTED operation can also exist without a journal, and a request_result's
payload/status/body can differ from its referenced operation. PR02 admission binds
those records and creates the required purchase effect; terminal-state checks and
foreign keys alone do not establish consistent business replay.

`reference/journal_fold.py` reads raw rows without importing production helpers and
compares every account's debit/credit/booked totals, including idle accounts.
The original frozen request separately determines the intended merchant and amount.
Snapshots for this check are consistent, and the purchase ownership check covers
an isolated purchase's before/after state. Neither result is a global correctness proof.

## Implemented purchase service

`ledgerops.db.admission.submit_purchase` requires an idle autocommit connection and
trusted typed principal. It takes the shared workspace barrier before reading mode,
checks customer ownership, reserves scoped request/business identities, locks
balances in UUID order, checks available funds and cumulative bounds, writes the
journal and relative projection deltas, then finalizes both stored responses.
It returns after COMMIT. Non-NORMAL mode yields retryable 503 without a saved claim.

Same canonical bytes under either identity replay the original result. Changed
bytes conflict and roll back the provisional claim. A stored operation's customer
must also belong to the principal before a result or conflict is disclosed. Replays
ignore later enablement/balance changes; new purchases require enabled accounts.
The merchant account in the command is the recipient, resolved within the workspace.
Authentication is the caller's responsibility, not proof obtained from a UUID.

Insufficient available funds and cumulative/version limit exhaustion persist 422
without a journal or balance updates. Success persists 201 with an immutable journal
ID and amount, not current balances. Terminal request/operation result comparisons
preserve JSON type differences such as true versus 1. Retained header and response
checks are not a replay-time audit of every historical line or balance projection.

Unexpected integrity faults propagate after rollback. Deadlock 40P01 and serialization
failure 40001 retry at most three complete transactions with unchanged identities.
Backoff is 10–50 ms then 20–100 ms, only after rollback restores an open IDLE connection.
Exhaustion raises retryable 503 with the last SQL error as cause. Lock timeout 55P03,
statement timeout/cancellation 57014 and other failures do not internally retry.
No business rejection is fabricated for contention or connection faults.

Connection loss during COMMIT can leave the outcome unknown. The service propagates
that exception; the caller reconnects and resubmits the same identities. Tests sever
a real COMMIT before it reaches PostgreSQL and after the server commits but before
its confirmation reaches the caller. They also kill a worker after committed service
return and before its business reply. Recovery creates or replays exactly one effect.
No automatic reconnect or durable client command store is implemented.

Controlled concurrent schedules cover two debits of 80 from 100, both identity replay/
conflict paths, and ascending UUID lock order when the merchant sorts first.
Independent fold and original-owner assertions check final money and identity counts.
The test schedules and three fault locations do not exhaust every possible failure.
No external payment/provider call occurs inside a purchase transaction.

## Retained later gates

- R2: at most one successful full reversal through a separate success relation;
  multiple rejected attempts retained. Full export/rebuild requires in-flight writer
  drain before activation. Database freeze does not prove an external send stopped.
- R5: approvals bind the current relevant evidence epoch; publication and execution
  serialize on the evidence scope. Hash identity alone does not keep approval current.

No reversal table/reference, provider, reservation lifecycle, AI, HTTP server or UI
is implemented through PR03. Future releases amend this contract explicitly.
