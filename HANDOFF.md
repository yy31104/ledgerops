# LedgerOps handoff

Updated: 2026-10-28. R1 and bounded local R2 acceptance complete, through R2-PR04.

## Status

| Field | Current fact |
|---|---|
| `implementation` | PR01 foundation plus PR02 Python purchase admission: authorization, workspace barrier, scoped identity claims, sorted balance locks, journal/projection writes, retained accepted/rejected results and replay. PR03 adds three-attempt whole-transaction retry and contention/transport recovery experiments. PR04 extracts four admission steps without behavior/test changes. R2-PR01 adds reader privileges, consistent export, offline structure/A/B checks, CLI and a test-only command model. R2-PR02 adds writer drain/freeze, preview, atomic live repair/audit/epoch, stable-ID recovery and explicit fresh-check resume. Separate resume correction3d4901f gates only structure/A and retains B/reserve reports. R2-PR03 adds original-merchant full reversal via shared admission/retry/replay and one success relation; exportv4 includes attempts/effects and keeps v1/v2/v3. PR04 adds integrated mixed schedules, mutations, actual exporter interruption and acceptance mapping without changing production bytes. No HTTP/authentication adapter. |
| `verification` | R2-PR04: 581 passed, zero failures/errors/skips (272 unit, 41 reference, 268 real PostgreSQL integration). Ruff, installed-package/SQL-resource check and wheel/sdist build passed. See Git-linked R2 evidence and historical R1 records. |
| `personal_ownership` | Yi accepted four R1 defaults by replying “沿用这组默认”. Independent explanation or modification has not been assessed. R2 decisions 2/4/5 accepted; 1/3 accepted with amendments. |
| `R2 planning` | Decisions adopted: retained-payload intent, no candidate generations. Docs commit 328733b precedes PR01 a067785; PR02 d825d41, correction3d4901f, PR03 82fa8ff and PR04 complete; R2 passes its bounded local acceptance. |

The repository remains on local `main`, with baseline commit
`feb458864c293d77c838913e720e878f09b96846` and a separate PR04 extraction commit.
R1-PR04 is `9add0b8fefd5fa7ad69abd842a02d5fa24baa046`; use
`git diff feb4588..9add0b8` for the pure extraction, excluding later planning documents.
R2 adoption commit `328733b871e12c5e4b096efe2db0a25793266123` precedes
the separate R2-PR01 implementation commit, identifiable with
`git log -1 --format=%H -- evidence/R2-PR01/2026-10-27/verification.json`.
The next separate PR02 commit is based on a067785 and is located by
`git log -1 --format=%H -- evidence/R2-PR02/2026-10-27/verification.json`.
The resume-policy correction is separate commit `3d4901f`; PR03 is based on it and
located with `git log -1 --format=%H -- evidence/R2-PR03/2026-10-28/verification.json`.
PR04 is based on 82fa8ff; locate its containing delivery commit with
`git log -1 --format=%H -- evidence/R2-PR04/2026-10-28/verification.json`.
No remote, deployment or tracker update is part of this delivery.
Chronicle already has its own baseline `be5df9a`, created by parallel work; this
session did not modify its code, index or in-flight review fixes.

## Authority and implemented design

The user authorized initial implementation, accepted [packet 1](docs/DECISION-PACKET-R1.md),
then explicitly requested PR03 and supplied the Opus R1 review. The baseline and
readability findings were completed in PR04. This round follows the accepted R2
packet, including the user's two amendments. The independent PR01 review then
explicitly authorizes PR02 and requires old-export compatibility. Its next review
authorizes the separate resume correction, then PR03. The following independent
review accepts both commits and authorizes PR04 integrated acceptance; no new
production behavior or demo exercise was requested. Older design-only and
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

## Reversal implementation

`submit_reversal` uses the same Principal, transaction orchestrator, request/business
claims, replay paths and bounded retry as purchase. Reversal has its own typed command
and route. Preclaim merchant authorization reads immutable original data without a
row lock; a new reversal then locks the original accepted purchase FOR UPDATE before
sorted balance rows. Validate original canonical purchase, accounts, exact journal
lines and response. Post new merchantD/customerC journal, retain every rejected
attempt, and insert unique purchase_reversal_effect only on success. Old purchase
stays accepted and unchanged. ALREADY_REVERSED is retained422; identity409 is not.

SQL005 enforces attempt/success type/scope bindings, success-only uniqueness and
accepted/rejected journal/relation final-state checks. Python and independent B
check exact business intent. Unexpected success23505 propagates and rolls back.
Stored ALREADY replay must find a committed success relation; historical insufficient
funds is never re-evaluated against current balance. Authentication remains caller's
responsibility. [Reversal usage](docs/REVERSAL.md) documents all boundaries.

## Verification and reproducibility

- [R2-PR04 acceptance](docs/R2-ACCEPTANCE.md) and
  [evidence](evidence/R2-PR04/2026-10-28/README.md): **581 passed** in44.625s,
  zero failures/errors/skips. Ten added actual PostgreSQL cases: two purchase/refund
  orders, four-way purchase/reversal/replay/freeze chain, different-original refunds,
  three mutations, two real exporter kills and disabled-customer404/no-retention.
  Current real lock waits and literal outcomes are checked with independent fold/B.
  Source/migrations/reference/fixtures and all previous tests/evidence are identical
  to82fa8ff; production review found no corrective change needed.

- [PR03 evidence](evidence/R2-PR03/2026-10-28/README.md): 571 passing tests in
  39.585 seconds; 123 new cases relative to the correction. All R1 financial tests
  unchanged. New service/core/schema tests verify the frozen trace, refused→funded→
  new-success transition, original merchant authority, no owner leakage on replay/
  cross-kind conflict, original-row immutability, original-before-sorted-balance
  locks and actual single-effect races. Eight SQL stages roll back, unexpected23505
  propagates, whole-transaction retry stays bounded, both wire COMMIT outcomes
  recover by original identity. All old frozen export formats pass new source and
  installed CLI. First full run failed only old PURCHASE-only capability assertion;
  corrected format4 assertion also checks empty reversal tables, then full run passes.
- [Separate resume correction](evidence/R2-RESUME/2026-10-28/README.md), 3d4901f:
  448 passed. Actual reserved and WRONG_OWNER cases resume with unchanged financial
  rows and immutable reports; repair still refuses. This supersedes the earlier
  PR02 resume restriction, not the historical evidence bytes below.

- [R2-PR02 evidence](evidence/R2-PR02/2026-10-27/README.md): 432 passing tests,
  including 50 new real PostgreSQL maintenance/CLI cases and 24 new pure format/
  audit cases. Observe actual writer→freeze lock dependencies, competing freeze IDs
  and same repair-ID races. Literal before/after checks include missing idle rows,
  versions/epoch and immutable complete audit. Wrong-owner/current-source/reservation
  guards, stale-preview substitution, four SQL fault stages and both actual wire
  COMMIT outcomes preserve atomicity and stable-ID recovery. Old v1 bundle bytes
  remain accepted; new v2 carries maintenance records. See exact run/tooling metadata.

- [R2-PR01 evidence](evidence/R2-PR01/2026-10-27/README.md): 358 passing tests in
  23.176 seconds; 121 new cases plus R1 regression. Actual reader-login tests verify
  SELECT-only authority outside READ ONLY transactions. An observed writer commits
  between table reads; REPEATABLE READ retains the old complete snapshot while a
  READ COMMITTED mutation fails the same expected-state assertion. Actual
  projection-only corruption fails A; consistent wrong-owner journal and projection
  fail B. Offline tests exercise retained-payload corruption, file integrity,
  byte/row limits and publication faults. Reversal-model tests are test-only.
  R1 admission and schema 001 are unchanged; migration 002 adds the reader role.

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
that production slice; its tests, SQL schema, migrations, fixtures and reference
implementation matched baseline. Later R2 slices add the changes recorded above.
All admission helpers participate in the same explicit
transaction; no new behavior or premature R2 abstraction was introduced.

[PR03 task](docs/tasks/R1-PR03.md) is complete for its bounded schedules and fault
locations. The service retries only 40P01/40001, at most three total attempts, sleeping
10–50 ms then 20–100 ms after IDLE rollback. Exhaustion preserves the last DB error as
cause. Timeouts, cancellation, integrity faults, connection loss and business results
are not internally retried. No automatic reconnection is performed; the caller owns
the connection. COMMIT connection loss remains outcome-unknown until resubmission
resolves persisted identity. No finite fault suite proves every failure schedule.

R2 [task contract](docs/tasks/R2.md) and [Chinese packet](docs/DECISION-PACKET-R2.md)
are adopted with the user's amendments: B verifies retained operation.payload,
with hand-frozen inputs only as extra test oracles; repair uses fresh transactional
recomputation plus before/after audit and epoch, without candidate generations.
Decisions 2/4/5 remain consistent JSON snapshot export, retained 422 ALREADY_REVERSED,
and original-merchant reversal authority. No repeated approval is required.

The user authorized a separate documentation commit followed by
[R2-PR01](docs/tasks/R2-PR01.md). PR01 now delivers bounded consistent export and offline
structure/projection/retained-request checks, plus an independent test command model.
Maintenance freeze/apply/resume is now delivered in [PR02](docs/tasks/R2-PR02.md);
production full reversal is delivered in [PR03](docs/tasks/R2-PR03.md), with
[PR04 integrated acceptance](docs/tasks/R2-PR04.md) complete. R2 passes the declared
bounded local core acceptance, mapped in [the matrix](docs/R2-ACCEPTANCE.md).
Git commit references replace new source SHA inventories; original evidence remains.

Use [export/verify instructions](docs/EXPORT-AND-VERIFY.md). Before migrations,
provision `ledger_reader` alongside the existing owner/app roles. The CLI uses
LEDGEROPS_EXPORT_DSN only for export; verification is offline. Reports separate
structure/A/B and reservation support. Nonzero reserves prevent rebuild eligibility.
Opaque OPENING funding intent is disclosed. Captured purchase payloads are B's
authority, not proof of original external intent. PR02 rechecks live sources
under the exclusive barrier; it never applies target balances from a preview file.
[Maintenance usage](docs/MAINTENANCE.md) documents the extra role, APIs, commands
and failure recovery. Preview ID is a caller-supplied audit reference, not an
authenticated file receipt. No command proves a human read a report. Historical
freeze/repair/resume replay returns saved metadata without acting on a new session.
Application remains FROZEN; resume checks structure/A again. The 2026-10-28 review
corrects its earlier excessive gate: B failure and nonzero reserved are saved in
resume_report but do not block reopening. Rebuild still requires B and zero reserves.
Migration004/exportv3 persist that report while retaining v1/v2 bundle compatibility.
This correction is committed separately as 3d4901f before PR03.
No internal maintenance retry/reconnect: SQL failures propagate, and COMMIT loss
requires reconnecting with original IDs. A healthy freeze may resume without repair.

Full source-package release acceptance, a persistent local demo DB, HTTP/token
verification and operational packaging must not be inferred from the service test count.

R2's success-only uniqueness, retained failed attempts and integrated schedules are
verified within the current contract. See the matrix for exact evidence/limits.
Disabled original customer: new reversal returns404 and retains no claims/attempt;
reactivation permits the same identity, historical authorized replay remains valid.

The lock argument is for current cooperating single-workspace/one-original paths;
it does not prove arbitrary SQL, future batches or all possible deadlocks absent.
Keep bounded retry/timeouts. A database freeze does not prove an external send stopped.
No external provider or hold lifecycle was added. R3/provider work is not started;
R5 current-evidence epoch and stale-approval rejection remain future requirements.
Optional make demo and personal self-test exercises remain a separate choice.

Present the next material decisions as one group of 3–5. Learning is tracked
separately and does not limit already authorized AI implementation.

## Preserved sources

`docs/reference/` remains byte-for-byte unchanged (17 checksums). Its
[provenance](docs/reference/PROVENANCE.md) identifies the original package. The frozen
100/80 purchase fixture retains its digest. Vault documents were read, not modified.
