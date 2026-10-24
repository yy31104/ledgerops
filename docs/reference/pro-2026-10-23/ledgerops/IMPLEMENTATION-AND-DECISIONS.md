# LedgerOps — releases, implementation slices and decisions

**Status:** proposed plan, not implementation or an adopted schedule. All paths/commands below are future repository conventions. No repository location or branch has been created. Human review bandwidth is limited; release scope is not capped by current independent coding ability. AI may implement multiple releases ahead, and personal-learning progress is not a software dependency.

## 1. Dependency order and review format

Logical dependencies: R1 posting/identity → R2 independent checking → R3 external recovery → R4 settlement reconciliation → R5 safe operations. R2's verifier may be developed alongside R1 from the frozen contract; R3's provider simulator can be developed independently against its protocol before integration. R4 algorithm prototypes can use frozen files before R3 is complete, but cannot claim end-to-end recovery coverage until integrated. R5 identity/permission scaffolding can start earlier. R6 export depends only on the evidence schemas it exports.

Chronicle has no logical dependency on these releases. Its initial temporal fixtures and implementation may proceed in parallel; the recommendation about human review concentration is in the [executive review](../00-EXECUTIVE-REVIEW.md). Do not silently convert that recommendation into a “finish LedgerOps R1 before Chronicle code” rule.

Each review packet contains: the relevant contract diff, 3–5 decisions below, a miniature trace, changed module map, actual evidence commands/results if available, open assumptions and rollback/reversal cost. Save three distinct status fields: `implementation`, `verification`, `personal_ownership`. Allowed values should reference facts, e.g. implemented at commit; verified by run ID under specified assumptions; explained/modified/debugged by the user with an exercise reference. Reading a debrief is not an independent-modification result. Verified software remains documentable while personal understanding is unfinished.

## 2. Proposed module boundaries

```text
src/ledgerops/api/{purchases,payouts,accounts,operations,reconciliation,actions}.py
src/ledgerops/domain/{money,identities,contracts,errors}.py
src/ledgerops/db/{pool,queries,admission}.py
src/ledgerops/recovery/{dispatcher,observations,resolver}.py
src/ledgerops/reconciliation/{inputs,matching,partitions,publication}.py
src/ledgerops/operations/{authorization,approvals,restore}.py
migrations/versions/<revision>.py
sql/{roles,tables,constraints,posting_functions}.sql
provider_sim/{api,store,contract,statements,faults}.py
tests/{unit,integration,failures,security,mutations}/
reference/{command_model,journal_fold,fixture_checker}.py
fixtures/{golden,provider,reconciliation}/
web/                         # R5, not needed for R1
evidence/                    # actual generated reports, never design placeholders
docs/{adr,runbooks}/
```

Keep `reference/` independent: importing production posting, matching or money-normalization helpers to calculate expected outcomes is prohibited. Shared external serialization libraries are acceptable only where they do not compute the financial oracle; document any shared assumptions.

## 3. R1 — transaction core and replay

### Reviewable slices

1. **R1-PR01: executable contract foundation.** Create a project skeleton, lock candidates after compatibility/license review, real PostgreSQL fixture, migration and runtime roles, typed money/identity schema, frozen opening/purchase fixtures and the first group-invariant test. No provider, worker, UI or broker.
2. **R1-PR02: single transaction admission.** Implement typed purchase entry point, request-result replay, operation uniqueness, complete journal and balance projection using explicit SQL; clean success, rejection and conflict paths.
3. **R1-PR03: concurrency and ambiguous reply.** Add barriers and process harness; run two debits of 80 from 100, simultaneous same key, whole-transaction retry and commit/reply-loss cases; add full internal reversal only with its stated scope.

Acceptance: L-AC1/2, L-DB1/2, L-ID1/2/3 and API cases relevant to purchases pass against real PostgreSQL. No account is initialized by a hidden balance overwrite. A reviewer can see the complete transaction boundary and no network I/O in it.

| User decision | Plain options and recommended default | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Accounting perspective | **Platform chart:** customer/merchant as liabilities, clearing as asset; alternative uniform signed-wallet abstraction | Chart is more explicit but requires normal-side understanding. High migration cost after persisted history; low before schema. | Hand-calculate opening 100, purchase 80, payout 62 and compare all three normal balances. |
| Posting authority | **Narrow DB entry points + deferred checks + revoked direct DML**; alternative app-only posting with direct DB writes | More SQL review, stronger runtime misuse barrier. Medium refactor cost before external users. | Attempt malformed balanced/unbalanced and unauthorized direct mutations under runtime role. |
| Concurrency | **Sorted account row locks at READ COMMITTED**; alternatives conditional update or SERIALIZABLE | Explicit serialization, hot-account waiting; alternatives trade retries or multirow complexity. Medium change cost. | Identical two-account workload/interleavings under both paths, compare correctness/waits. |
| Identity and rejection retention | **Stable business UUID plus scoped request key, lifetime retention including business rejection**; alternative finite request cache | Extra storage but avoids expiry-driven ambiguity. High contract cost once clients exist. | Same operation/new key after rejection/funding, committed reply loss and workspace separation. |
| First reversal scope | **One full internal reversal only if funds available**; alternative omit reversal until R2 | Adds useful semantics without pretending spent funds can be recovered. Low before public API, medium after. | Merchant spends half; reversal must reject without changing original journal. |

## 4. R2 — independent evidence and rebuild

Slices: R2-PR01 independent map model/literal accounting corpus; R2-PR02 journal/hold export and drift checking; R2-PR03 maintenance rebuild plus deliberate wrong-owner/missing-entry/projection mutations. Acceptance: frozen examples agree, mutation checks fail for the intended reason, a corrupted projection is detected and cannot be activated as a successful rebuild, and old immutable evidence remains inspectable.

| User decision | Options and recommendation | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Independent specification | **Literal human-reviewed scenarios + separately coded command model**; alternative another implementation mirroring SQL | More thought, substantially less shared implementation bias. Low code reversal, high epistemic importance. | Insert balanced wrong merchant error in both operation metadata and journal; fixture still catches it. |
| Snapshot extraction | **Materialized export from one REPEATABLE READ transaction**; alternative sequence watermark | Slight extraction cost; avoids late-commit omission. Medium change cost. | Hold low-sequence transaction open while higher-sequence commits; verify snapshot membership. |
| Rebuild activation | **Maintenance freeze, shadow generation, compare, then swap**; alternative online catch-up | Temporary unavailability accepted for local lab; online algorithm adds complexity. Medium later extension. | Post during attempted rebuild; freeze prevents race, failed rebuild leaves old generation active. |
| Evidence retention | **Pin released manifests and files; explicit GC for unreferenced staging only**; alternative delete old reports aggressively | More disk, stronger replay. Low initially, high after deletion. | Remove a file from a pinned manifest in a test copy; reader reports corruption/unavailability. |

## 5. R3 — uncertain provider outcomes

Slices: R3-PR01 independent SQLite provider with durable key/tombstone and golden contract tests; R3-PR02 reservation+intent transaction and conservative dispatcher; R3-PR03 authenticated observations/resolution; R3-PR04 failure matrix and recovery after provider success. Acceptance: L-EXT1/2/3 pass with separate processes/stores; no timeout-only release, no changed provider identity, no automatic correction of contradiction.

| User decision | Options and recommendation | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Provider negative authority | **Explicit close-if-absent tombstone**; alternative unknown remains unresolved indefinitely until outside evidence | Makes a narrow provable closure contract; stronger than many real providers. High semantic cost if removed. | Race late PUT against closure; exactly one serialized outcome, no post-release execution. |
| Fee responsibility | **Merchant pays fixed quoted fee included in gross hold**; alternative platform absorbs via expense account | Simple explicit balance effects; fee quote changes need new operation. High accounting cost after history. | Principal 60 / fee 2 from 80; success leaves 18 and clearing decreases 62. |
| Provider implementation | **Separate SQLite file/process**; alternative second PostgreSQL instance | Smaller independent implementation; not a provider scalability model. Low adapter replacement cost. | Kill/restart provider while ledger remains up; provider effects survive independently. |
| Callback authority | **Authenticated immutable terminal observation with exact identity/sequence; conflicts freeze automation**; alternative polling-only confirmation | Faster resolution with richer validation; polling-only simpler but slower. Medium protocol cost. | Duplicate, stale, same ID with different payload and contradictory terminal traces. |
| Hold lifetime | **Evidence-driven release, no expiry after possible send**; alternative pre-send cancellation only | Uncertainty may immobilize funds and require operator attention. This is honest loss of availability for safety. | Repeated 500/NOT_FOUND for a successful hidden payout must never increase availability. |

## 6. R4 — versioned settlement and batch experiments

Slices: R4-PR01 explicit provider statement/group/fee fixtures and single-process matcher; R4-PR02 immutable manifests and atomic publication; R4-PR03 partitioning complete groups; R4-PR04 optional reassignment/leases with sink fencing; R4-PR05 measured 1/2/4-process comparisons. Acceptance: correct semantics for all difference categories; no partial/mixed result publication; stale generation rejected; performance report uses identical input/result digests and reports failed runs.

| User decision | Options and recommendation | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Group matching | **Explicit settlement references/membership, ambiguity quarantined**; alternative fuzzy amount/time matching for suggestions | Lower apparent match rate, auditable correctness; suggestions may be added read-only. Medium contract cost. | Equal amount paid to two merchants: a missing reference must not select a guess. |
| Initial algorithm | **Hash join/group aggregation**; alternative external sort/merge | Simple expected linear runtime, memory proportional to keys. Low change cost behind a fixed result schema. | Skewed million-row corpus if resources allow; measure RSS, temporary disk and exact differences. |
| Worker coordination | **Single process first, leases only with reassignment**; alternative immediate generic scheduler | Preserves the scaling experiment without an unrelated scheduler. Medium cost once multiple workers exist. | Kill a partition worker and require another to reclaim it; that requirement triggers fencing. |
| Result visibility | **Immutable run IDs and atomic completed pointer**; alternative mutable live report | Readers see complete evidence; the current pointer may lag. Medium UX adaptation cost. | Kill after half the partitions and after a file write; an incomplete run cannot become current. |
| Parallelism criterion | **Measure 1/2/4 processes, preserving complete groups and fixed RAM/DB caps**; alternative workers-per-CPU heuristic | Honest contention/skew behavior, no promised speedup. Low choice cost. | Compare read/parse/match/write phases and straggler partition time. |

## 7. R5 — operator workflow and recovery drills

Slices: R5-PR01 authenticated local identities/scope matrix; R5-PR02 narrow investigation UI with evidence links; R5-PR03 version-bound maker/checker action; R5-PR04 upgrade/backup/restore drill including provider-ahead and missing-history cases. Acceptance: permission/state validation occurs on server, stale approval blocked atomically, audit remains readable, restore performs no uncontrolled dispatch and clearly reports unresolvable gaps.

| User decision | Options and recommendation | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Identity scope | **Fixture identity service locally, real identity provider before hosting**; alternative immediate cloud authentication | No external account dependency for experiments; local tokens do not establish production security. Medium integration cost. | Forge role/workspace input and prove the server ignores it. |
| Approval binding | **Proposal, evidence and target versions with distinct maker/checker identities**; alternative approving only a case ID | More reapproval after revisions, prevents stale intent. Medium API cost. | Update evidence or spend target funds after approval; execution must fail. |
| Correction authority | **Typed allowed actions using normal posting primitives**; alternative arbitrary SQL/manual balance edits | Fewer shortcuts, preserves invariants. High risk if relaxed. | Attempt an unknown-outcome release or journal edit through the operator role and verify rejection. |
| Restore contract | **Freeze until provider, command and key histories are verified; report irrecoverable gaps**; alternative automatic redispatch | May require investigation; avoids repeating external effects. High operational importance. | Restore before admission while the provider has paid; outbound PUT count must remain zero. |
| Deployment extent | **Local Compose/native first; optional single VM after separate approval**; alternative managed cloud cluster | Supports artifact reproducibility, with no high-availability claim. Low code/medium operational change cost. | Clean start on another machine with no existing credentials and documented limits. |

## 8. R6 — optional read-only evidence export

Slices: R6-PR01 stable versioned export manifest; R6-PR02 compatibility/golden-import tests with Chronicle; R6-PR03 optional narrow evidence view. Main agent implementation remains in Chronicle. No write API or database credential is shared.

| User decision | Options and recommendation | Trade-off / reversal cost | Smallest discriminator |
|---|---|---|---|
| Integration transport | **Explicit immutable export files**; alternative live-query API | Portable pinned evidence, delayed freshness. Low adapter cost. | Chronicle reproduces an old report after LedgerOps changes, without LedgerOps running. |
| Export scope | **Selected synthetic records and provenance**; alternative entire database dump | Smaller stable contract; avoids leaking internals/credentials. Medium schema change cost. | Importer rejects an unsupported major version or units and preserves owner identity. |
| Cross-project authority | **Read-only analysis, no operational callbacks**; alternative agent-triggered corrections | A human assesses actionable findings; the control plane stays isolated. High risk if relaxed. | An agent attempting to submit a correction has no available tool or credential. |

## 9. First exact implementation task

After engineering review, create **R1-PR01 as a coherent initial slice**: typed money/identity contracts; migrations for workspace/account/projection/operation/request/journal/entry; runtime privileges and deferred complete-journal checks; fixtures for clearing/customer funding of 100 and the intended purchase of 80; and an integration test proving that a one-sided journal cannot commit. Establish those tests before implementing the successful posting path in R1-PR02. This is a testable starting artifact; it does not require completing a prerequisite course first.

Proposed future targets, **not currently implemented or executed**:

```bash
make bootstrap             # lock/install reviewed dependencies; no paid service
make db-up                 # local PostgreSQL only
make migrate
make test-unit
make test-integration TEST=tests/integration/test_atomic_journal.py
make test-integration TEST=tests/integration/test_runtime_privileges.py
make test-contracts
make evidence RELEASE=R1
```

Later targets: `make provider-up`, `make test-failures`, `make test-mutations`, `make reconcile-fixture`, `make benchmark WORKLOAD=small`, `make restore-drill`. Each must document destructive scope and use disposable test namespaces by default. Never execute a restore against an existing database outside the test environment merely because a target exists.

Before external actions, separately confirm publication, remote infrastructure, external accounts/secrets/costs and any data that is not synthetic. This research authorizes none of them. The next implementer should inspect [CONTRACTS §2–5](CONTRACTS.md), challenge the accounting/identity/authority assumptions, then implement the first slice under recorded provisional defaults or the user's explicit choices.
