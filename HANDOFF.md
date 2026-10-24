# LedgerOps handoff

Updated: 2026-10-25. Active scope: local LedgerOps through R1-PR04.

## Status

| Field | Current fact |
|---|---|
| `implementation` | PR01 foundation plus PR02 Python purchase admission: authorization, workspace barrier, scoped identity claims, sorted balance locks, journal/projection writes, retained accepted/rejected results and replay. PR03 adds three-attempt whole-transaction retry and contention/transport recovery experiments. PR04 extracts four admission steps without behavior/test changes. No HTTP/authentication adapter. |
| `verification` | Final PR04 run: 237 passed, zero failures/errors/skips (91 unit, 18 reference, 128 real PostgreSQL integration). Ruff, installed-package/SQL-resource check and wheel/sdist build passed. See the separate evidence records. |
| `personal_ownership` | Yi accepted four defaults by replying “沿用这组默认”. Independent explanation or modification has not been assessed. |

The repository remains on local `main`, with baseline commit
`feb458864c293d77c838913e720e878f09b96846` and a separate PR04 extraction commit.
Use `git log --oneline` to locate that delivery and `git diff feb4588..HEAD` to review it.
No remote, deployment or tracker update is part of this delivery.
Chronicle already has its own baseline `be5df9a`, created by parallel work; this
session did not modify its code, index or in-flight review fixes.

## Authority and implemented design

The user authorized initial implementation, accepted [packet 1](docs/DECISION-PACKET-R1.md),
then explicitly requested PR03 and supplied the Opus R1 review. This round follows
the local-baseline and readability findings; optional demo and R2 work are separate. Older design-only and
initialization instructions describe historical work. [ADR 0002](docs/adr/0002-r1-foundation.md),
[ADR 0003](docs/adr/0003-purchase-admission.md),
[ADR 0004](docs/adr/0004-contention-and-recovery.md) and the
[working contract](docs/working/R1-CONTRACT.md) record current choices.

`ledgerops.db.admission.submit_purchase` owns an explicit READ COMMITTED transaction
and requires an idle autocommit connection. It returns only after COMMIT. It takes
the shared workspace barrier before mode or identity writes, checks customer
ownership, and authorizes the stored operation again before exposing a retained
result or identity conflict. Principal authentication remains the trusted caller's
responsibility; arbitrary UUIDs are not proof of identity.

Success writes two journal lines, relative deltas and both results atomically.
Insufficient available funds and cumulative/version limits persist rejection without
financial effects. Replay retains original status/body across funding and account
enablement changes. New purchases need enabled accounts. JSON comparisons distinguish
true/1 and false/0. Large cumulative integers do not use Decimal arithmetic.

The schema alone still permits missing journals for accepted operations and divergent
request/operation payloads/results. The service creates bound records and detects
specific malformed retained outcomes. Its replay checks are not a historical audit
of line ownership/projection correctness. Database-valid writes can still be business-
wrong through the trusted runtime credential; independent checks remain essential.
The migration owner can alter guards and is outside those protections.

## Verification and reproducibility

- [PR04 evidence](evidence/R1-PR04/2026-10-25/README.md): existing 237-test regression,
  with all tests unchanged. The 219-line orchestrator is now 63 lines with four
  single-purpose helpers (16/24/15/110 source lines). Independent AST review confirms
  extraction equivalence after SQL whitespace normalization; pre-existing functions
  outside the orchestrator are identical. Commit `feb4588` preserves the received
  baseline, and the subsequent implementation commit preserves the reviewed diff.

- [PR03 evidence](evidence/R1-PR03/2026-10-23/README.md): 16 new integration tests plus
  regression. Two purchases of 80 against 100 produce exactly one success and one saved
  rejection; four identity races replay/conflict after the real uniqueness wait.
  Reverse account UUID order is observed with a NOWAIT probe.
  A real 40P01 deadlock and injected 40001 failures (during writing and COMMIT) verify
  whole-transaction retry, unique transaction IDs, stable command identities and
  single effects. Three failures exhaust the attempt budget; 23514 is never retried.
  Actual cancellation 57014 and lock timeout 55P03 leave no claims, release the barrier,
  and allow same-identity recovery after unlock.
  Three actual process/wire faults distinguish committed/reply lost, COMMIT never
  forwarded, and committed/confirmation withheld. Reconnect resolves state using
  the original business identity. Source/artifact hashes and JUnit bind this run.

- [PR02 evidence](evidence/R1-PR02/2026-10-23/README.md): 56 new tests plus PR01 regression,
  JUnit, exact versions, source/artifact hashes and a transparent failed-setup run.
- [PR01 evidence](evidence/R1-PR01/2026-10-23/README.md): earlier 165-test foundation run.
  Its manifests describe historical source/build bytes, not current files.

Actual `ledger_app` credentials drive service tests on pinned PostgreSQL 18.6. They
cover success/replay/rejection, scope and owner isolation, five SQL failure stages
through deferred COMMIT, malformed retained state, large integer limits and workspace
barrier ordering. A historical PR02 corruption-fixture setup error was fixed by executing
pending deferred checks before restoring its test-only trigger changes; tests were
then rerun. No production guard was relaxed for the tests. During PR03, the test proxy cleanup
needed cooperative read shutdown: cross-thread socket close alone could leave a
relay blocked. After fixing it, recovery and full-regression checks passed.

Use `make bootstrap`, `make check`, `make test`, `make build`. For no-Docker work use
`make test-unit`. Missing Docker fails rather than skipping. Integration resources
are disposable and target only newly created containers/databases. Tests import
current source directly; package checks avoid this Mac's hidden editable `.pth` issue.
No persistent database is provisioned; opening journals remain explicit fixture SQL.

## Current boundary and next work

[PR04 task](docs/tasks/R1-PR04.md) is complete: only admission.py was refactored in
production; tests, SQL schema, migrations, fixtures and reference implementation
remain identical to baseline. All helpers participate in the same explicit
transaction; no new behavior or premature R2 abstraction was introduced.

[PR03 task](docs/tasks/R1-PR03.md) is complete for its bounded schedules and fault
locations. The service retries only 40P01/40001, at most three total attempts, sleeping
10–50 ms then 20–100 ms after IDLE rollback. Exhaustion preserves the last DB error as
cause. Timeouts, cancellation, integrity faults, connection loss and business results
are not internally retried. No automatic reconnection is performed; the caller owns
the connection. COMMIT connection loss remains outcome-unknown until resubmission
resolves persisted identity. No finite fault suite proves every failure schedule.

The source's next release is R2 independent checking/export, drained maintenance
rebuild and success-only full reversal. A new task contract should make that slice
concrete before coding. This round does not start R2. Full source-package release
acceptance, a persistent local demo DB, HTTP/token verification and operational
packaging must not be inferred merely from the current service test count.

Retain later gates: R2 success-only reversal uniqueness with rejected attempts kept,
maintenance freeze API, writer drain and full export/rebuild; R5 current evidence
epoch and racing stale-approval rejection. A database freeze does not prove an
external send stopped. No reversal/provider/hold lifecycle was added.

Present the next material decisions as one group of 3–5. Learning is tracked
separately and does not limit already authorized AI implementation.

## Preserved sources

`docs/reference/` remains byte-for-byte unchanged (17 checksums). Its
[provenance](docs/reference/PROVENANCE.md) identifies the original package. The frozen
100/80 purchase fixture retains its digest. Vault documents were read, not modified.
