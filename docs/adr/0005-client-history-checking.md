# ADR 0005: check recorded client histories for isolation anomalies

Date: 2026-11-05. Status: accepted, test-only.

## Context

The concurrency tests in [ADR 0004](0004-contention-and-recovery.md) assert literal
business outcomes. Examples: one success and one retained refusal for 80 + 80 against
100; final balances; an independent journal fold. Those oracles check the end state.
They do not check how the transactions got there.

A race can produce correct totals by accident. Admission uses relative SQL deltas
(`credits=credits+%s`), so if the balance row lock disappeared, many interleavings
would still produce the right sums. The decision "are funds sufficient?" would then
be made on a stale read.

## Decision

Record client-observed histories and check them offline, following Elle: Kyle Kingsbury
and Peter Alvaro, *Elle: Inferring Isolation Anomalies from Experimental Observations*,
PVLDB 14(3), 2020. Anomaly names follow Atul Adya, *Weak Consistency: A Generalized
Theory and Optimistic Implementations for Distributed Transactions*, MIT, 1999.

- **Traceable versions.** Elle needs to know the order in which versions of an object
  were installed. `ledger.account_balance.version` increases by exactly one per write,
  so `(account, version)` identifies one write and the order is known.
- **Recording without touching `src/`.** A psycopg cursor class installed through
  `cursor_factory` records the balance rows admission fetched and the versions its
  updates installed. Commit status comes from `pg_xact_status`.
- **Graph.** ww, wr and rw edges between committed transactions. Searching narrowest
  first, a cycle is reported as G0, G1c, G-single or G2-item. Non-cycle checks cover
  G1a, G1b, lost update, duplicate versions and non-adjacent read-modify-write.
- **Existing tests unchanged.** `tests/history/test_existing_concurrency.py` imports 7
  existing concurrency tests (11 cases with parameters) and reruns them with recording.
  Each must produce at least one committed balance transaction and no anomaly.
- **Proof it can fail.** A test-only mutant strips `FOR UPDATE OF b`. Two purchases of
  30 then keep correct totals, yet the checker reports `lost-update`, `non-adjacent-rmw`
  and `G-single`. A locked control with the same inputs is clean.

## Consequences

- Recording a history makes the check depend on how the transactions interleaved,
  not only on the final balances.
- The model covers only balance rows in purchase and reversal admission. Extending it
  to payout holds needs the payout SQL markers and a model for the `reserved` column.
- Recognising statements by SQL text is brittle. The teardown assertion that some committed transaction both read and wrote a balance
  turns a silent mismatch into a failure.
- About 10 seconds are added to `make test`.

Details and the "Break it" experiments: [tests/history/README.md](../../tests/history/README.md).
