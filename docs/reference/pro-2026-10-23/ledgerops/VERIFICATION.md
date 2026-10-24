# LedgerOps — verification and falsification plan

**Evidence status:** all tests, failpoints, workload sizes, CLI targets and expected outcomes below are proposed. None has been implemented or run for LedgerOps. Public-source tests were read, not reproduced. The acceptance contract is bounded to the specified PostgreSQL, provider and filesystem assumptions; it does not prove universal correctness, regulatory compliance, bank-grade reliability or exactly-once execution across arbitrary systems.

## 1. Independent evidence design

Use four complementary checks, because no one oracle covers the complete problem:

1. **Frozen hand-calculated cases.** A human reviews the accounting convention and the seven traces in [CONTRACTS](CONTRACTS.md#8-worked-golden-traces) before implementation. Expected entries and ownership are literal fixture data, not exports from the production journal.
2. **Simple command reference model.** A small single-threaded map-based model operates on business commands and explicit admissible outcomes. Its data structure and calculation code are separate from SQL functions and production helpers. It does not implement workers, retry middleware, ORM models or SQL matching queries. It calculates expected liabilities/asset/held funds and detects invalid transitions.
3. **Independent provider evidence.** The simulator stores its own commands and effects, generates statement versions independently, and can deliberately violate the contract in labeled fault cases. It never imports the ledger's “actual” outcomes to manufacture agreement.
4. **Real-component interleavings and failures.** PostgreSQL constraints, lock waits, commit/reply ambiguity, process death and file publication are exercised against real components. Python mocks only isolate protocol parsing and pure paths; passing them does not establish database or network behavior.

Two AI-written implementations can share a mistaken requirement. Have the user/reviewer calculate a tiny case in their own terms and compare it to both implementations. Preserve unresolved specification disagreements; don't change expected results merely to match code. No AI grader may declare monetary equality when deterministic checks disagree.

## 2. Requirements → tests → evidence

| Requirement | Test IDs and expected result | Boundary | Evidence saved |
|---|---|---|---|
| L-AC1 amount/currency | `T-MONEY-01`: reject float, exponent, overflow, fee sum overflow, unsupported currency, negative/zero transfer; canonical accepted values round-trip exactly | Pure parser + PostgreSQL bound test | Input/output fixtures, rejected SQLSTATE |
| L-AC2 conservation/normal sides | `T-BOOK-01`: opening, purchase and payout entries agree with literal table; customer liabilities need contra-account; hold changes availability only | Pure model + real DB | Expected postings, queried journal and balances |
| L-DB1 full atomic journal | `T-ATOMIC-01`: one-line/zero-line/unbalanced header fails at commit; halfway exception leaves no journal/projection/result | Real PostgreSQL, runtime and privileged test roles separated | Transaction history and constraint diagnostics |
| L-DB2 write privilege | `T-AUTHDB-01`: runtime INSERT/UPDATE/DELETE/TRUNCATE/direct generic-posting call/schema shadowing rejected; permitted business entry point works | Real database roles | GRANT inventory and attempted statements |
| L-DB3 drift and rebuild | `T-DRIFT-01`: privileged projection corruption found; rebuild equals independent fold; failed rebuild never activates; account with zero/missing activity included | Real DB + separate verifier | Before/after generations and drift report |
| L-ID1 scoped replay | `T-ID-01`: same-key same bytes returns same status/body/effect; same key changed payload: 409; new key same operation no second effect; scoped keys don't collide | Real DB/API | Persisted result IDs and response corpus |
| L-ID2 simultaneous key | `T-ID-02`: 16 concurrent identical submissions produce one admitted operation and one journal; bounded wait may yield retriable 503, then replay resolves | Real PostgreSQL/API | Per-call begin/end/outcome, unique counts |
| L-ID3 concurrent debits | `T-CONC-01`: two distinct debits of 80 from 100 → one successful effect, one stable business rejection, 20 remaining; either winner permitted | Real PostgreSQL with two connections/barriers | Statement/interleaving history, final records |
| L-ID2 commit ambiguity | `T-CRASH-01`: kill before commit→no effect; kill after commit before reply→same-identity replay, no second effect; kill DB backend during commit→query to resolve | Real process + DB | Failpoint marker, process status, replay result |
| L-ID2 resource cleanup | `T-CANCEL-01`: cancel a request or break its connection, then verify the next operation progresses and the pool has no leaked checked-out connection or transaction | Real API/driver/PostgreSQL | Pool state, transaction state, cancellation and cleanup logs |
| L-EXT1 independent dimensions | `T-EXT-01`: HELD+UNKNOWN is preserved through timeout/500/lagging NOT_FOUND; no automatic release/new key | Real provider process + ledger | Provider and local histories, submitted key list |
| L-EXT2 negative finality | `T-EXT-02`: race PUT and close-if-absent; either existing execution or permanent tombstone, never release then later execute | Real independent SQLite provider + DB | Serialization trace, tombstone and effect count |
| L-EXT3 callback handling | `T-EXT-03`: duplicates/old sequence no duplicate effect; invalid signature/digest/amount/owner quarantined; contradictory terminal messages stop automation | Pure crypto/parser + real effects | Original message hashes and incident record |
| L-EXT3 remote success/local crash | `T-EXT-04`: provider effect persists, dispatcher dies, lookup/replay consumes exactly one hold/posts once | Independent processes/stores | Both stores' evidence, process-failure trace |
| L-EXT3 eventual progress | `T-LIVE-01`: after faults stop and one worker/provider path is healthy, all cases with available authoritative terminal evidence resolve; genuinely unresolved cases remain labeled | Independent processes/stores | Remaining work and unresolved-evidence categories, bounded observed recovery time |
| L-REC1 semantic matching | `T-REC-01`: equal totals/wrong merchant still WRONG_OWNER, plus missing/duplicate/amount/currency/timing/fee/ambiguity cases | Frozen files + independent reference | Expected difference IDs/categories/evidence refs |
| L-REC2 snapshot publication | `T-REC-02`: kill at file and DB boundaries, never visible partial run; pinned old run survives new statements; missing/corrupt file refuses report | Real filesystem + PostgreSQL | File inventory/hashes and run state histories |
| L-REC2 publication/GC race | `T-GC-01`: pause after rename before metadata commit, run GC, then resume or kill publisher; live publication is protected by the object-registry lock, crash leaves an unreferenced orphan, active reader pins prevent deletion | Real filesystem + PostgreSQL with controlled barriers | Lock history, object registry, pin state and file inventory |
| L-REC3 fencing | `T-FENCE-01`: old generation rejected after reassignment and after expiry under strict policy; duplicate correct publication idempotently recognized | Real DB two workers | Claim/renew/submit trace, accepted generation |
| L-OPS1 operator controls | `T-OPS-01`: cross-workspace denied, maker cannot approve own action, revoked checker permission denied, changed evidence/state rejects stale approval | API + actual server identity fixture | Redacted audit and permission matrix |
| L-OPS2 restore | `T-RESTORE-01`: restore old local DB while provider stays ahead, zero blind dispatches; recover known identity or remain blocked on missing archives | Real backup/restore/provider | Backup inventory, reconciliation of gaps, outbound request count |

Assertions must distinguish “no reply” from “no effect.” Count **business effects and distinct IDs**, not just successful HTTP responses. A lost reply may correspond to an accepted effect; a 503 may coexist with another client's eventual success. Persist rejected admitted commands separately from malformed unauthenticated submissions.

## 3. Stateful/property testing

Generate a bounded account population and commands: funding, purchase, full reversal, payout request, dispatch observation, duplicate observation, terminal resolution and query. The model uses arbitrary-precision integers; the SUT's declared limits are tested explicitly. Invariants after each completed transition include balanced journals, correct normal sides, no negative available liability, one effect per business ID, projection/hold fold equality, terminal hold transition uniqueness, and owner fidelity.

Generate dependencies rather than only independent random commands: spend after reservation, reverse after merchant spent funds, key conflict after success, late success after close-if-absent, conflicting callback after consumption, corrupt evidence after approval. Shrink failing sequences and save the minimized case. Property-test seeds reproduce generated inputs; they do not reproduce the real operating system or PostgreSQL schedule. TigerBeetle achieves stronger deterministic replay by controlling clock/network/disk in its simulator [L-02](../01-REFERENCE-REGISTER.md#l-02); this project must not borrow that claim.

A proposed 10,000-event command corpus is a workload target, not proof or an observed success. Use multiple documented seeds, edge-value corpora, both valid and invalid actions, and record admitted/rejected/transport-unresolved counts. A failed invariant invalidates performance comparisons for that run.

## 4. Controlled histories and bounded checking

Add explicit test-only barriers around `after_request_claim`, `before_account_lock`, `after_postings_before_commit`, `after_commit_before_reply`, `after_attempt_commit_before_send`, `after_provider_commit_before_reply`, `after_observation_insert_before_resolution`, `after_result_file_fsync_before_publish`, and `after_lease_reassignment`.

Release binaries must refuse to enable failpoints unless a local test configuration is explicitly set. Failpoints cannot be request parameters from ordinary users. Record request invocation/response times, connection/transaction IDs where observable, operation IDs, statements/failpoints, result bodies and final contents. Use causal barriers for named schedules rather than sleeps. A watchdog terminates stuck tests and records timeout separately from a business failure.

Optional bounded history checker: for histories up to 8 completed/uncertain operations over at most 3 accounts, enumerate serial orders consistent with observed real-time precedence, including whether ambiguous requests committed, and compare against the independent state machine. Record the bound and checked operation subset. Passing this checker supports those histories, not a proof of general linearizability. Larger workload tests can check invariants without pretending exhaustive serial-order search.

## 5. Mutation checks: can the suite detect plausible defects?

| Deliberate defect | Required failing check |
|---|---|
| Remove account lock / use stale absolute balance write | Concurrent-debit journal/projection outcome test fails |
| Write response record after commit in a second transaction | Commit/reply-loss replay or orphan effect test fails |
| Compare key without payload / hash only without canonical field coverage | Changed-amount/changed-owner replay test fails |
| Always change merchant to another valid account | Frozen business-owner test fails although sum check passes |
| Treat timeout/NOT_FOUND as failure and release hold | Delayed success/close race test fails |
| Generate a new provider key after a network error | Provider effect count / submitted-key identity test fails |
| Consume reservation outside journal transaction | Crash-between-resolution-steps invariant fails |
| Ignore callback contradiction / accept event ID with changed bytes | Contradiction quarantine test fails |
| Split a settlement group across arbitrary partitions | Golden grouped-settlement result differs |
| Check token before transaction but omit sink predicate | Late worker publication test fails |
| Approve by case ID without evidence/state versions | Stale-approval test fails |
| Restore starts dispatcher automatically | Restore drill observes outbound request and fails |

These mutations are temporary test variants on isolated fixtures, never a published production setting. “Mutation killed” requires the intended assertion to fail; a syntax error or fixture setup crash does not count. The Formance lock-release regression is particularly instructive: a prior test passed broken code because killing the backend freed the lock anyway; the stronger test also checked discarded-connection state [L-03](../01-REFERENCE-REGISTER.md#l-03). Apply that lesson to test adequacy rather than copying Go implementation details.

## 6. Performance experiments

Do not begin with a target transactions-per-second promise. First establish correctness, then publish a reproducible comparison:

| Experiment | Workload and comparison | Measurements and limitations |
|---|---|---|
| Online posting | 1/4/16 clients; uniform vs hot account (e.g. 80% of debits from 1% of accounts); successful and insufficient-fund requests reported separately; row locks vs conditional update | Throughput of admitted effects, p50/p95/p99 end-to-end latency, DB lock/pool waits, retries, CPU/RSS/WAL. Transport replays not counted as new throughput. |
| Reconciliation | 10^5 then 10^6 rows if hardware permits; group sizes 1/10/1000, heavy-tail merchant ownership, missing/duplicate/late/revised records and fee cases | Parse/match/write time separately; peak RSS, scanned bytes/rows, temporary disk, result digest equality. Cold/warm cache labeled. |
| Parallel matching | Same immutable corpus/rules, 1/2/4 processes; partition complete groups, fixed global connection/RAM caps | Speedup versus 1 process, startup/serialization/I/O contention, skew/straggler impact. Four workers do not imply4× speedup. |
| Recovery | Known injected interruption, measured time until all resolvable outcomes settled; unresolved cases reported separately | Recovery latency/requests, oldest unresolved age, duplicates avoided within provider contract. A forcibly closed case must not masquerade as naturally resolved. |

Save machine model/CPU cores, memory, OS/filesystem, Python/driver/PostgreSQL/SQLite versions, image digests, commit, schema, configuration, dataset manifest, warmup, repetitions, raw samples and failure counts. Use medians and distributions across repeated runs; do not discard failed runs. Measure overhead added by verification separately. If insufficient hardware prevents a million-row trial, publish the smaller verified scale and resource limit honestly.

## 7. Evidence package and acceptance

Proposed evidence path in the future repository:

```text
evidence/<release-id>/<run-id>/manifest.json
evidence/<release-id>/<run-id>/environment.json
evidence/<release-id>/<run-id>/input-manifest.json
evidence/<release-id>/<run-id>/process-history.ndjson
evidence/<release-id>/<run-id>/assertions.json
evidence/<release-id>/<run-id>/database-summary.json
evidence/<release-id>/<run-id>/provider-summary.json
evidence/<release-id>/<run-id>/mutation-results.json
evidence/<release-id>/<run-id>/metrics.csv
evidence/<release-id>/<run-id>/limitations.md
```

Each evidence manifest identifies test code version, SUT code version, fixture revision and actual command. Never fill it with the design's desired outcomes. A README links to the latest executed report and clearly labels optional/unverified features. Implementation, verification and personal-understanding status are separate fields; a missing personal exercise does not hide verified software behavior or stop further implementation.

Release software acceptance requires the relevant deterministic cases, real-component invariants and defect-detection checks to pass on a clean environment, plus documented limitations. Coverage percentages and test counts are supplementary, not acceptance criteria. Personal review can ask the user to predict an interleaving, locate the SQL boundary, modify a rule and debug an unseen counterexample, recording what actually happened without presenting a completed reading as mastery.
