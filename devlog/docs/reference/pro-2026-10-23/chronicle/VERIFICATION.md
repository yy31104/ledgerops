# Chronicle — verification plan and evidence contract

Status: all project tests and benchmarks below are **proposed and unexecuted**. Upstream source inspection is recorded separately in [01-REFERENCE-REGISTER.md](../01-REFERENCE-REGISTER.md). There is no current repository, CI status or personal-competence result to report.

## 1. What verification can establish

The target is reproducible evidence that the implemented contracts hold for the declared inputs, failure schedules, environment and supported operations. It is not a proof of all possible histories, unlimited data scale, perfect data truth or economic causal inference.

Use four distinct evidence layers:

1. **Frozen, hand-calculated examples.** Commit input events and expected outputs before production logic. A reviewer independently checks source column selection, calendar boundaries, units and arithmetic. Tests using a copied production formula are not independent.
2. **Simple reference model.** Python records plus a full scan for each query, explicit membership sets, rational arithmetic and full recalculation. No production SQL, source normalizer or invalidation helper is imported. Share schema types only if doing so cannot encode the very rule being tested; record that dependency.
3. **Real-component tests.** Actual PostgreSQL, filesystem operations and separate processes with recorded barriers/failpoints. They establish transaction/object lifecycle behavior that mocks cannot.
4. **Adversarial mutation and evaluation.** Deliberately remove guards or corrupt inputs, then require the relevant test to detect a wrong observable outcome. AI quality needs actual model trials; mocks validate wiring only.

Two independent implementations can still inherit a mistaken specification. The specification review therefore includes the expected real BLS revision narrative versus level arithmetic, a different hand-built synthetic case, and a reviewer challenge to time/rights assumptions. XTDB's inspected tests provide an instructive combination of concrete anchors and differential comparison, not an oracle to import wholesale. [C-02](../01-REFERENCE-REGISTER.md#c-02)

## 2. Requirements-to-tests matrix

| Requirement | Proposed tests | Independently expected result | Real/mocked boundary |
|---|---|---|---|
| C-RQ01 time meanings | C-T01 initial/source revision; C-T02 future revision; C-T03 actual local snapshot; C-T08 precision | 100 before revision,103 after; S1 remains100; a newly imported old archive never creates a historical local snapshot | Pure scan + real SQL; fake clock only in explicitly synthetic fixture |
| C-RQ02 identity/units | C-T04 tie/conflict; C-T05 withdrawal; C-T06 unknown time; C-T07 unit change | Conflicting equal revision quarantined; withdrawn does not expose older value; unknown time excluded from strict cutoff; unit conflict blocks calculation | Schema tests + real import transaction |
| C-RQ03 historical selection | C-T02; C-T06; C-T24 differential histories | Preserve every requested observation with value or explicit missing status; never borrow another series' row | Independent scan vs actual PostgreSQL; no SQLite substitution |
| C-RQ04 publication | C-T09 crash matrix; C-T10 competing publishers; C-T11 GC/pins; C-T12 corruption | Only old or complete new snapshot visible; replay committed ID; unpublished data invisible; corruption never becomes empty/current data | Real DB/files/processes; failpoints coordinate order |
| C-RQ05 incremental calculation | C-T14 footprint/correction/deletion; C-T15 code/schema invalidation | Canonical output/evidence equals independent full result; absence dependencies retained; every affected downstream month recomputed | Independent Fraction-based full model vs production incremental executor |
| C-RQ06 bundles/replay | C-T13 old replay; C-T20 restore; C-T23 migration | Old context/code/input hashes remain fixed; parser repair in a new interpretation generation preserves old replay; mismatched backup blocks affected snapshot; unsupported schema fails clearly | Real DB/files; retained fixture code versions; no external package download required in test |
| C-RQ07 explanation matrix | C-T16 data×code interaction | Four cells retained; 10/12/20/24 gives data2+code10+interaction2=total14; missing cell yields incomplete attribution | Pure arithmetic + actual orchestration with two tiny transformations |
| C-RQ08 API/context/resource limits | C-T10; C-T18 scope; C-T19 cancellation | Context never drifts, scope cannot be supplied by caller, execution actually stops on cancel/deadline | API/DB integration; process cancellation measured |
| C-RQ09 rights and source provenance | C-T17 rights inheritance; C-T21 archive parser | FRED disabled by policy; denied source cannot enter model/export via derived values; source table/units/columns fixed | Frozen HTML extracts/synthetic parser fixtures; actual public acquisition is separate, allowed adapter step |
| C-RQ10 evaluated AI | C-T22 validators/graders plus held-out AI tasks | Exact numbers/citations/cutoff membership, appropriate abstention and no policy bypass | Mocks only software paths; real model runs required for quality claims |
| C-RQ11 independent operation | C-T25 clean start without LedgerOps | Synthetic reports and comparison work without ledger services/network/key | Fresh local environment with network disabled after dependencies installed |
| C-RQ12 truthful evidence | C-T26 evidence manifest audit | Implementation, verified behavior and personal understanding fields remain independent | Artifact lint/reviewer check; never infer mastery from test count |

## 3. Golden corpus

Store a proposed `tests/fixtures/golden/` corpus with an explicit manifest and reviewer-authored `expected.json`. Source rows are small enough to inspect in full. The fixture generator must not compute expected values by calling production code.

### G1 — initial and revised past

Synthetic December=90, January=100 source date February 10, January revised to 103 source date March 15; S1 has first batch, S2 both. Expected January employment deltas 10/13, immutable S1=10, strict cutoff before March 15 excludes 103. Query a complete requested month grid, including a month with no data: the absent row survives with a reason.

### G2 — late ingestion and source reconstruction

Simulate receipt on March 18 while source availability is March 15. A `source_as_of` query over the later archive can reconstruct 103 at March 16; an earlier actual-system snapshot cannot. In the real adapter case, import 2024 documents at the actual 2026 runtime and prove that no local 2024 snapshot is synthesized. The tests must label fake clocks as simulated.

### G3 — source ordering and withdrawal

- Same key, identical content: no new value; provenance occurrence allowed.
- Same key, different value: quarantine candidate, pointer unchanged.
- Same-day distinct versions without evidenced order: quarantine; changing import order must not change the published answer.
- Later withdrawal: returned status is `withdrawn`, not the previous value.
- Later missing value: returned status is `missing`; source semantics must identify missing versus explicit withdrawal.

### G4 — precision and units

An availability date with no clock cannot satisfy an intraday request. Unknown availability cannot pass the source cutoff. Give a known older value 100 and a higher-order unknown-time 103: strict as-of must report uncertainty/null instead of confidently returning 100. An older unknown revision below an eligible newer winner is a separate case. A series unit change from thousands to persons is rejected by report compatibility until a versioned conversion is selected. Distinguish a new index base from a mere display-format change. Use December/January values where a factor1000 error is conspicuous.

### G5 — reviewed real archive

The tiny direct BLS/Board corpus in [DATA-RIGHTS-AND-AI-EVAL.md](DATA-RIGHTS-AND-AI-EVAL.md) is both a source-parser test and a human cross-check. Expected January employment change 353 → 229 thousand, difference −124. Expected production mean3 `102.666666666667`→`102.633333333333`; canonical-output difference `-0.033333333334`. Twelve decimals are arithmetic representation, not measurement precision.

Independently verify the BLS revised-change narrative as a second source location. Never use the selected table parser to generate its own expected fixture. Do not treat four publications as complete ALFRED-style vintage coverage.

### G6 — dependency halos

Production November 97, December 100, January 100 initially yields January mean 99; revision of January to 103 yields 100. Include February/March outputs to prove a changed January affects all windows containing it. Include a missing February later backfilled; a result previously missing must become complete. Include a withdrawal and verify stale numeric results disappear in the new result. Include a year-over-year metric whose denominator changed 12 months earlier, plus a tiny nonzero denominator that triggers typed output-range failure without a partial published result.

A parser-repair C-T23 trace deliberately swaps a source column in generation 1, records the defect, reparses retained bytes into generation 2, and checks that generation 1 remains replayable and is never silently relabelled as correct. Generation2 records actual reinterpretation time and original source provenance.

### G7 — controlled code change

Old code `f(x)=x`, new code `f(x)=2*x`, old data 10, new data 12. Require all four runs, data 2, code 10, interaction 2, total 14. Drop one old executable and expect typed incomplete attribution, never fabricated values. Change only the cutoff or knowledge mode between proposed contexts: a general comparison may label that difference, but controlled 2×2 attribution must reject the mismatched conditions. Add incompatible unit/schema variants that cannot be compared.

## 4. Real failure schedules

Inject failpoints only in a dedicated test build/configuration; production request parameters cannot choose arbitrary failpoints. Save the exact test commit, PostgreSQL settings, failpoint name, event order and process IDs. A random seed generates input/order requests; it does **not** make PostgreSQL/OS scheduling deterministic.

| Failpoint | Test coordination and required observation |
|---|---|
| After writing temp object, before fsync/rename | Kill importer; current snapshot unchanged; temp file never exposed as evidence |
| After durable promotion, before marking registered object ready | Kill publisher; orphan is either safely adopted by hash verification or collected after staging closure |
| After normalized batch commit, before snapshot publication | Reader of current snapshot cannot see staged revision despite larger IDs/timestamps |
| After snapshot rows inserted, before pointer update/commit | Separate connection sees only previously committed snapshot; kill aborts transaction |
| After publish commit, before reply | Retry same request identity returns original snapshot; membership and pointer version not duplicated |
| Two candidates ready against same parent | Barrier before dataset lock; exactly one pointer CAS wins; other receives base conflict and no lost input |
| Reader pinned old snapshot while new snapshot publishes | All report cells and evidence remain old; new source cannot leak into half the report |
| Publisher paused after promotion while GC or same-hash writer runs | Active staging ticket and shared object advisory lock prevent deletion/overwrite; age alone does not establish abandonment |
| GC attempts to delete referenced object | Object-reference/reader checks reject collection; no file deletion occurs |
| Kill collector after `deleting`, before/after unlink | Restart completes or diagnoses tombstone; no phantom ready object |
| Delete/truncate one referenced object using test admin | Reader returns corruption/unavailable, not latest or empty; exact affected closure recorded |
| Cancel blocking query/report subprocess | Actual backend/subprocess stops within proposed budget; result publication remains absent |
| Restore database with a deliberately incomplete object closure | Health indicates blocked snapshots and lists missing hashes; normal serving does not resume for them |

Do not claim physical power-loss durability from SIGKILL alone. Fsync ordering, filesystem type and PostgreSQL durability settings are recorded; a power-cut experiment would be a separate supported-environment test. A process-fault test can establish that the application protocol handles those observed failures, not that hardware always preserves bytes.

## 5. Mutation plan: the suite must detect wrong behavior

Each mutation is an isolated test branch/fixture substitution, never a production option. Expected mutant outcome is a failed relevant assertion, not necessarily a crash.

| Mutation | Detector |
|---|---|
| Remove source availability filter | C-T02 selects future revision incorrectly |
| Use ingestion time as source availability | C-T03 reconstruction versus actual-system trace disagrees |
| Filter after left join and drop all-future requested row | C-T06 cardinality/missing-status assertion fails |
| Pick greatest UUID/import order for same-day tie | C-T04 shuffled ingestion yields a forbidden winner |
| On withdrawal, query previous non-null value | C-T05 fails explicit withdrawn status |
| Treat missing as zero or use last three rows across a calendar gap | C-T07/G6 arithmetic/status fails |
| Determine snapshot membership from maximum sequence ID | C-T09 staged committed batch leaks into published view |
| Advance pointer before validating object closure | C-T09/C-T12 readers observe incomplete snapshot |
| Let GC ignore report/reader references | C-T11 old replay fails; test also asserts collector's refusal |
| Recompute only changed month, omit following-window outputs | C-T14 independent full recomputation differs |
| Ignore a missing-input dependency | C-T14 late backfill fails to update old missing result |
| Reuse cached result without transform/rights/cutoff identity | C-T13/C-T17 returns wrong or forbidden bundle |
| Omit data×code interaction | C-T16 total decomposition fails |
| Validate citation existence but not context/scope/cell support | C-T22 adversarial claim passes incorrectly; detector must fail |

A mutation that survives is a review finding: either add a meaningful test or narrow the claimed contract. A high mutation percentage by itself is not a correctness proof; report which semantic defects were detected.

## 6. Benchmarks and performance evidence

No timing, throughput or speedup is asserted here. Proposed size families are `10^4`, `10^5`, `10^6` revisions, scaled down if the actual machine cannot run them safely. Use generated synthetic data for scale, not repeated downloading of public archives.

| Workload | Design and measured quantities |
|---|---|
| C-B01 historical selection | Uniform and highly skewed revisions per observation; many revisions concentrated in 1% of series; latest versus old cutoff; absent/future-only rows. Compare independent scan, unoptimized SQL and indexed SQL. Measure wall time, p50/p95 over repeated runs, CPU, peak RSS, rows returned, examined rows, PostgreSQL buffer hit/read/temp blocks. |
| C-B02 incremental break-even | One changed month, 100 scattered changes, long backfill, withdrawals, unit/code changes. Include dependency-discovery, copying unaffected output/evidence and publication time. Compare total work against full recomputation; report cases where incremental loses. |
| C-B03 concurrent readers and publication | 1/4/8 bounded readers with importer, skewed query sizes and cancellation. Count successful/failed/cancelled requests separately; measure pool wait and snapshot conflicts. Every success must satisfy snapshot identity, not just return HTTP 200. |
| C-B04 optional analytical engine | Only after C-B01 identifies a relevant limitation: same exact corpus/formulas/canonical outputs in PostgreSQL and a separately version/license-reviewed engine. Include conversion/export/verification cost and memory, not scan timing alone. No required adoption. |
| C-B05 evidence/replay overhead | Cold/warm object verification, hash cost, bundle creation and old result replay. Report bytes read versus logical rows; don't call PostgreSQL buffer access counts exact physical disk bytes. |

Environment manifest: OS/architecture, CPU model/core count, RAM, filesystem/container limits, storage class, Python/PostgreSQL versions/image digests, dependency lock hash, test commit, PostgreSQL settings, dataset seed/hash, distributions, concurrency, warm/cold cache procedure and repetition count. Do not call an OS-cache-reset procedure “cold” unless it was actually controlled. Report variability and failures; no cherry-picked single best run.

A proposed interactive budget can be chosen at C4 review, after measuring the actual machine and report size. Numerical thresholds in that review are acceptance targets, not prior achievements. Correctness gates performance: a faster result with a leaked revision is a failed experiment.

## 7. AI verification boundary

The complete held-out protocol, trial schema, malicious-source tasks, labeling and metrics are in [DATA-RIGHTS-AND-AI-EVAL.md](DATA-RIGHTS-AND-AI-EVAL.md). The following gates apply to integration:

- B0 deterministic template is the baseline and works without a model.
- Tool mocks check schemas, cancellation, budgets and error branches only. They establish no answer-quality improvement.
- Real model evaluation is optional until authorization and budget are selected; record provider/model/version/settings, trial cost/latency and environment. No paid call is authorized by this research.
- Exact numeric/citation/context/rights checks run outside the model. Prompt instructions alone are not a boundary.
- Hold-out labels and malicious hidden variants are not fed back into prompts during reported evaluation. If tuning uses them, create a new hold-out set and disclose the change.
- A model may contain future facts in its parameters. Restricting retrieved documents is not enough; published factual claims must be evidence-bound, and plausible unsupported claims must be measured.

## 8. Evidence pack and three independent statuses

Each release produces a proposed `evidence/<release>/<run-id>/` directory using the shared contract in [02-CROSS-PROJECT-CONTRACT.md](../02-CROSS-PROJECT-CONTRACT.md):

- `manifest.json`: release/run identity, code commit and links to all evidence components, with explicit `pass/fail/not_run` results.
- `environment.json`: runtime versions, settings, machine/resource description and dependency locks.
- `input-manifest.json`: exact input/oracle hashes, synthetic/real provenance and rights references.
- `process-history.ndjson`: ordered failpoint/process events; empty with an explicit not-applicable reason for pure tests.
- `assertions.json`: requirement/test IDs, actual expected/observed outcomes and evidence locations.
- `requirements.json`: test IDs and which contract was exercised.
- `golden-diff.json`: expected/actual canonical values, statuses and evidence identity.
- `failures/`: ordered histories, failpoint log, minimized counterexample, actual stderr without secrets.
- `plans/`: query plans and resource metadata for performance work only when run.
- `mutation-report.json`: specific mutation→detector→outcome.
- `release-review.md`: consequential decisions, limitations, unexpected observations and next remaining risk.

Status fields are separate: `implementation`, `verification`, `personal_ownership`. A verified AI-assisted feature can be described honestly even while personal learning remains unfinished. Personal status is updated only from an actual explanation/change/debugging demonstration; there is no one-release lead cap or requirement to pass it before AI writes further software.

Review questions, without answer keys: Can the engineer identify why day-level source availability cannot justify an intraday cutoff? Can they repair the all-future-candidates join bug without dropping requested rows? Can they change mean3 to a twelve-month window and update invalidation correctly? Can they diagnose a database restore whose manifest points to missing files? Can they explain why a hash-valid source number can still be wrong? These are evidence opportunities, not a new daily tracker.

## 9. Proposed clean-start targets

These names are requirements for a future repository; none exists or has run yet:

```bash
make bootstrap
make dev-up
make db-migrate
make demo-synthetic
make test-contracts
make test-postgres
make test-faults
make test-mutations
make verify-bundle
```

`make demo-synthetic` must not need internet, an API key, LedgerOps or a paid model after dependencies are installed. `make test-faults` operates only on isolated disposable volumes/database names and cannot destroy a developer's ordinary data. Optional archive/model/benchmark targets are separate and explicitly opt-in. Passing output must be captured from the actual future implementation before being claimed.
