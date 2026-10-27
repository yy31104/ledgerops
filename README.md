# LedgerOps

A local laboratory for synthetic transaction integrity, replay and recovery.
**R1-PR01 through R1-PR04 are implemented locally:** typed money/identity, PostgreSQL
safeguards, an explicit-SQL purchase service, bounded transaction retries, and
concurrency / response-loss recovery tests with independent financial checks.
All financial examples use synthetic PLN minor units.

## Run locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), make and a running Docker engine.
The lockfile uses psycopg 3.3.6; tests pin PostgreSQL 18.6 by image digest. Docker may
need to download that image on the first run.

```sh
make bootstrap         # install the locked environment
make check             # Ruff plus installed-package / SQL-resource checks
make test              # complete suite, including real PostgreSQL
make test-unit         # domain and independent-reference tests; no Docker
make test-integration  # actual PostgreSQL tests
make build             # wheel and source distribution
```

Integration tests create one uniquely named disposable container on an ephemeral
localhost port, with random credentials and a fresh database per test. They remove
their own resources afterward and never use an existing application database.
Missing Docker fails the suite instead of skipping tests.

Tests load current `src/` explicitly. The package check uses a non-editable install:
on this Mac, Python ignores hidden `.pth` files, making editable-only import checks
unreliable. Build archives include migration SQL and exclude private reference docs.
No persistent database or HTTP server is provisioned in these slices.

## Purchase service

`ledgerops.db.admission.submit_purchase(conn, principal, command, request_key)` takes
an idle autocommit psycopg connection, trusted typed principal, validated
`PurchaseCommand` and scoped `RequestKey`. The caller is responsible for authenticating
the workspace/client/actor context. It returns `AdmissionResult` after COMMIT.
The [service integration test](tests/integration/test_purchase_admission.py) gives
executable inputs and checks independently calculated results.

The transaction takes the shared workspace barrier, authorizes the customer,
reserves request/business identities, locks balances in account-ID order, then
writes the journal, relative balance deltas and both stored results atomically.
Same bytes replay the original result; changed bytes conflict. Authorization also
covers the stored operation, so knowing its ID cannot reveal another customer's result.
Insufficient available funds persists a stable rejection; new funding requires a
new operation ID for a new decision. Success contains no mutable current balance.

The PR04 extraction keeps the transaction orchestrator to 63 source lines: workspace
admission, request-key replay, business-ID replay and new purchase posting each have
an explicit helper. Request/business claim order and the COMMIT boundary remain
visible in the orchestrator. [PR04 evidence](evidence/R1-PR04/2026-10-25/README.md)
compares this extraction to the committed R1 baseline without changing tests.

## Retry and recovery

Deadlock 40P01 and serialization failure 40001 retry at most three whole transactions
with unchanged identities. Each rollback must restore an open IDLE connection before
backoff (10–50 ms, then 20–100 ms). Lock timeouts and query cancellation are returned as
unretained retryable errors; arbitrary integrity faults propagate unchanged.

Connection failure can leave COMMIT outcome unknown. The service propagates it and
the caller reconnects with the same business ID and command. Tests sever the actual
COMMIT before PostgreSQL receives it, withhold acknowledgement after it commits,
and kill a committed worker before its business reply. Recovery returns or creates
one effect according to committed state; no automatic reconnect is hidden in service.

## Evidence and boundaries

[PR03 verification](evidence/R1-PR03/2026-10-23/README.md) records **237 passing tests**,
including 128 real PostgreSQL integration cases. The new cases force balance/identity
waits, a real deadlock, retry exhaustion, cancellation, lock timeout and the three
controlled connection/process faults. PostgreSQL lock relations and wire-frame
observations establish the tested schedules; timeouts are not treated as success.

[PR02 verification](evidence/R1-PR02/2026-10-23/README.md) covers the actual service,
including injected SQL failures through deferred COMMIT, identity/scope isolation,
retained-result corruption and workspace barrier ordering. The earlier
[PR01 record](evidence/R1-PR01/2026-10-23/README.md) remains historical evidence.

The frozen example funds 100 and purchases 80: clearing/customer/M1/M2 end at
100/20/80/0. Independent tests detect both missed projection updates and a transfer
to the wrong merchant even when journal and projection agree. Database-valid writes
can still be business-wrong; the trusted runtime credential can bypass service rules.
Replay consistency checks do not re-audit every historical entry or projection.

These bounded schedules and fault locations do not exhaust every possible failure.
Connection loss remains potentially ambiguous; never substitute a new identity.
There is no automatic reconnect, durable client command store, HTTP authentication adapter,
provider, reversal, hold lifecycle, deployment or production-readiness claim.

## R2 planning

The [R2 task contract](docs/tasks/R2.md) and
[five-item Chinese decision packet](docs/DECISION-PACKET-R2.md) are proposed for
review. They cover independent export/checking, writer-drained maintenance rebuild,
and success-only full purchase reversal. **No R2 code is implemented or decision
recorded as approved.** Confirm/change the packet before starting implementation.
R2 is split into four local commits; verification will reference Git revisions.

## Continue here

- [Current handoff](HANDOFF.md): implemented / verified / personal ownership.
- [Accepted decision packet](docs/DECISION-PACKET-R1.md): four defaults confirmed by Yi.
- [Foundation ADR](docs/adr/0002-r1-foundation.md), [purchase ADR](docs/adr/0003-purchase-admission.md)
  [retry/recovery ADR](docs/adr/0004-contention-and-recovery.md) and
  [working contract](docs/working/R1-CONTRACT.md).
- [PR02 task](docs/tasks/R1-PR02.md), [PR03 task](docs/tasks/R1-PR03.md) and
  [PR04 extraction task](docs/tasks/R1-PR04.md).
- [Agent rules](AGENTS.md) and [source provenance](docs/reference/PROVENANCE.md).

The repository has local Git commits and no remote. The received R1 implementation
is preserved in baseline `feb4588`; the PR04 extraction is the next independent
commit. Subsequent R2 planning is a documentation-only commit. PR numbers identify
work slices; no GitHub pull request, deployment or tracker update has been made.
