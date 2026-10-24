# Reference register — evidence, transfer boundaries and reuse decisions

**Observation date:** 2026-10-23. The linked public primary sources were inspected read-only. Repository source, tests, licenses and revision metadata are distinguished from current documentation and practitioner reports. No upstream code was run, no project implementation exists, and no passing experiment is claimed. A repository commit is a source-reading pin, not automatically the dependency version selected for implementation.

## Evidence vocabulary

| Category | What it establishes | What it does not establish |
|---|---|---|
| Inspected source | The specified code/test/license text was read at the named revision | That its tests pass here, or that its guarantees transfer to our code |
| Documented behavior | The publisher describes behavior under stated assumptions | Correct application use or unconditional operational guarantees |
| Practitioner/maintainer report | A named author or review records a problem, causal mechanism and fix | Independent reproduction, prevalence across all deployments, or a continuing unfixed defect |
| Proposed design | An opinionated recommendation for these projects | User/Codex approval or an implemented feature |
| Proposed falsification | A concrete future test that could expose a wrong design | A test result, benchmark or proof already obtained |

## Mechanism reading map

| Design question | Read first | Intended use |
|---|---|---|
| Booked versus pending amounts and hostile accounting tests | [L-01](#l-01), [L-02](#l-02) | Design reference; no second authoritative ledger database |
| Idempotency, cancellation and tests that pass broken code | [L-03](#l-03), [L-05](#l-05), [R-05](#r-05) | Semantic tests and resource boundaries |
| Independent settlement evidence and explicit grouping | [L-06](#l-06), [L-07](#l-07) | Narrow reconciliation workflow |
| Historical eligibility, missing rows and temporal oracles | [C-01](#c-01), [C-02](#c-02) | Selection and verification design |
| Why snapshots and artifact versioning are not the entire product | [C-04](#c-04), [R-01](#r-01), [R-02](#r-02) | Existing-tool baseline and limits |
| Data acquisition, precision and source-specific rights | [D-01](#d-01) through [D-07](#d-07) | Synthetic default plus bounded direct-source corpus |
| Evaluating the evaluator | [A-01](#a-01), [A-02](#a-02), [A-03](#a-03) | Numeric/context regressions and balanced tasks |
| Version/license/operational assumptions | [L-09](#l-09), [R-03](#r-03), [R-04](#r-04), [U-01](#u-01) | Dependency and exclusion evidence; actual locks remain future work |

The register summarizes transfer decisions rather than redistributing upstream code or article text. No substantial third-party code is included in this package. The initial application should use ordinary unmodified dependencies and separately authored business/temporal logic; any later adaptation must revisit the exact revision's notices and obligations.

## LedgerOps mechanisms and operational lessons

**Observed:** 2026-10-23. Selected repository files were retrieved read-only from public GitHub at the immutable revisions below and inspected locally. No reference code was executed, no tests reproduced, and no dependency installed. Documentation and practitioner reports are not independently reproduced production guarantees. Downloaded source was used only for inspection and is not included as application code.

<a id="l-01"></a>
### L-01

**TigerBeetle accounting and pending transfers — design reference, not dependency.** Inspected commit [`0450c32b2c3c3ab6114f3402cc601341cc8192aa`](https://github.com/tigerbeetle/tigerbeetle/commit/0450c32b2c3c3ab6114f3402cc601341cc8192aa), dated 2026-10-14 in repository metadata. The release page also identified 0.17.9 at [`cc1c06a924e49b11089c521b2209d34c92caaf18`](https://github.com/tigerbeetle/tigerbeetle/commit/cc1c06a924e49b11089c521b2209d34c92caaf18). The inspected main commit is not asserted to equal that release. Dated commits/releases are maintenance evidence, not a support guarantee.

Exact source: [`src/state_machine.zig`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine.zig#L3693), `create_transfer`, balance checks around L3886, pending/posted updates around L3910, and `post_or_void_pending_transfer` at L4036. Tests: [`create/lookup 2-phase transfers`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine_tests.zig#L1744) and [`per-transfer balance invariant`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/src/state_machine_tests.zig#L2423). Design: [versioned two-phase transfers](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/docs/coding/two-phase-transfers.md).

**Supports:** pending and posted totals differ; resolution validates the referenced pending transfer and its transition. **Transfer:** explicit accounting states and counterexample tests. **Does not transfer:** consensus, performance, deterministic scheduling, or knowledge of remote payment execution. **Local falsification:** duplicate consumption, conflicting reservation identity, timeout release followed by hidden success.

**License verified:** [LICENSE](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/LICENSE), Apache-2.0. No code reuse occurred. Later adaptation must preserve required license/notices, identify modifications and check file-specific terms and dependencies; this is not legal approval for an unspecified derivative.

<a id="l-02"></a>
### L-02

**TigerBeetle testing — design reference.** Same revision/license as L-01. Inspected [`docs/internals/vopr.md`](https://github.com/tigerbeetle/tigerbeetle/blob/0450c32b2c3c3ab6114f3402cc601341cc8192aa/docs/internals/vopr.md), VOPR/Assertions and Checkers. It documents controlled clock, network and disk behavior and replay identity based on seed plus commit. Transfer recorded failpoints, independent checks and minimized traces; do not claim a random seed replays ordinary PostgreSQL/OS scheduling. Local falsification: run identical generated inputs under different forced interleavings and accept all contract-valid winners.

**First-person failure lesson:** matklad, [Simulation Testing For Liveness](https://tigerbeetle.com/blog/2023-07-06-simulation-testing-for-liveness/), 2023-07-06, An Example/Teaching the VOPR/Resonance. The author reports that eventual healing or restart could hide persistent nonprogress, including repair-target resonance. Testing changed to preserve a healthy core while freezing other faults and requiring progress. This is a historical reported lesson, not a claim of a current bug. LedgerOps should stop injecting failures, keep a recovery path healthy and check that resolvable work drains; restarting everything until a test passes is inadequate. No consensus algorithm is copied.

<a id="l-03"></a>
### L-03

**Formance ledger — versioned comparison, not dependency.** Inspected release v2.4.16, commit [`64ee64d812cfbf2e20b7d2aa5a97057a67792272`](https://github.com/formancehq/ledger/commit/64ee64d812cfbf2e20b7d2aa5a97057a67792272), [released 2026-10-21](https://github.com/formancehq/ledger/releases/tag/v2.4.16). Source: [`internal/posting.go`](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/internal/posting.go), Posting/Reverse/Validate; [`api_idempotency_test.go`](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/test/e2e/api_idempotency_test.go#L50), repeated versus conflicting payloads; [`store.go`](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/internal/storage/ledger/store.go#L165), LockLedger; [`store_lock_test.go`](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/internal/storage/ledger/store_lock_test.go), cancellation/broken-connection regressions.

**Supports:** explicit postings, replay/conflict tests and bounded lock cleanup. **Transfer:** semantic test categories and resource-release assertions. **Does not transfer:** Go/Bun code, amount domain, error codes or retention rules. **Local falsification:** cancel a request or kill a DB backend, then verify both renewed progress and released/discarded pool resources.

**License verified:** [v2 LICENSE](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/LICENSE), MIT, copyright Formance Solutions. Substantial adaptation would retain copyright and permission notices. No adaptation occurred.

**Major-version trap:** current main commit [`136eca51b8edce4d9063713494cccc81463929cd`](https://github.com/formancehq/ledger/commit/136eca51b8edce4d9063713494cccc81463929cd), dated 2026-10-23, identifies v3 Raft/Pebble in its [README](https://github.com/formancehq/ledger/blob/136eca51b8edce4d9063713494cccc81463929cd/README.md). Its retrieved recursive tree contained no LICENSE path. Rights to adapt that rewritten revision were not established; do not infer them from v2 MIT. Exclude v3 code adaptation/dependency pending clarification. Do not describe v2 PostgreSQL internals as current v3 architecture.

**Maintainer failure lesson:** [PR 2133](https://github.com/formancehq/ledger/pull/2133), merged into release/v2.4 on 2026-10-13 and listed in v2.4.16. Request cancellation could prevent advisory unlock. The fix uses a bounded detached context and discards broken connections. An initial test passed broken code because killing the backend freed the lock; the stronger assertion checked connection disposal. This defect is fixed in the inspected release. A local mutation should demonstrate the same distinction between apparent progress and leaked resources.

<a id="l-04"></a>
### L-04

**PostgreSQL 18 — dependency behavior, documented guarantees.** [Transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html), Read Committed/Serializable; [explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html); [CREATE FUNCTION security](https://www.postgresql.org/docs/18/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY); [CREATE TRIGGER](https://www.postgresql.org/docs/18/sql-createtrigger.html). Versioned manual checked 2026-10-23; the shared runtime register supplies license/pin evidence.

**Supports:** statement snapshots, target-row predicate re-evaluation, retry requirements, sequence behavior and constraint-trigger/security-definer facilities. **Transfer:** explicit transaction and privilege patterns. **Does not transfer:** correct business invariants or atomic writes to external files merely because the database is ACID. **Local falsification:** two competing debits, late commit below an allocated sequence watermark, incomplete journal commit and schema-shadow attempts. Our SQL fragments remain unexecuted.

<a id="l-05"></a>
### L-05

**Stripe — error/idempotency design reference.** [Advanced error handling](https://docs.stripe.com/error-low-level), Network errors/Server errors; [Idempotent requests](https://docs.stripe.com/api/idempotent_requests), checked 2026-10-23. Current documentation explicitly distinguishes API v1 and v2; their replay and retention rules must not be merged.

**Supports:** network/server errors may accompany effects, so safe retry requires the provider contract. **Transfer:** preserve identity, retain uncertainty, obtain authoritative evidence. **Does not transfer:** Stripe access, API guarantees, windows or execution. **Local falsification:** provider commits then returns 500 or drops its response; recovery must not double pay or release the hold merely because of that error. No API call/account was used.

<a id="l-06"></a>
### L-06

**Stripe Ledger — dated practitioner account.** Ilya Ganelin, [Ledger: Stripe's system for tracking and validating money movement](https://stripe.dev/blog/ledger-stripe-system-for-tracking-and-validating-money-movement), 2024-02-16, semantic data store and data-quality sections. The author describes heterogeneous producers with incomplete, delayed or incorrectly modeled records and layered checks/investigation.

**Transfer:** owner, completeness and timing checks against independently identified records; preserve corrective history. **Does not transfer:** organizational scale, availability metrics, customer adoption, audit or compliance status. **Local falsification:** wrong merchant with equal totals, and an omitted business command while the journal remains balanced. Promotional scale figures are not project targets. This is an authored engineering account, not a released dependency or reproduced production experiment.

<a id="l-07"></a>
### L-07

**Goldman Sachs TxB — documented integration pattern.** [Integration via API, Step 6: Reconcile](https://developer.gs.com/docs/services/transaction-banking/best-practices-api-recon/), checked 2026-10-23; Intraday Payables for Batch Payments/EOD Reconciliation/Exception Management. The examples include grouped debit records and common payment references.

**Transfer:** explicit grouping and business-cutoff semantics. **Does not transfer:** banking access, universal settlement contracts, one bank row per internal transfer, or a junior hiring rubric. **Local falsification:** split batch rows, duplicate member allocation, missing reference and wrong owner despite a balanced aggregate.

<a id="l-08"></a>
### L-08

**Psycopg 3 — dependency transaction mechanism.** [Transaction management](https://www.psycopg.org/psycopg3/docs/basic/transactions.html), Autocommit transactions/Transaction contexts, checked 2026-10-23. The live page identified 3.3.7.dev1; this is a documentation reference, not the installation candidate. The shared register pins inspected stable candidate 3.3.6 and its LGPL license.

**Supports:** implicit transaction pitfalls and explicit transaction contexts. **Transfer:** autocommit connection plus an explicit business unit of work. **Does not transfer:** our API's cancellation/connection-loss behavior until tested. **Local falsification:** an earlier SELECT must not silently expand the posting transaction; exceptions roll back; cancellation releases or discards resources.

<a id="l-09"></a>
### L-09

**SQLite — provider dependency through Python's standard library.** [Copyright/public-domain statement](https://www.sqlite.org/copyright.html), [WAL documentation](https://www.sqlite.org/wal.html), checked 2026-10-23. Record the actual bundled version at bootstrap (`sqlite3.sqlite_version`); none was installed by this research.

**Transfer:** a small independent provider store with serialized writes and documented local-filesystem assumptions. **Does not transfer:** distributed availability or banking finality. `close-if-absent` is our proposed protocol, not a SQLite feature. **Local falsification:** concurrent submit/closure and a crash after provider commit retain one key outcome under the configured durability settings. Audit bundled notices separately. The core SQLite statement does not license unrelated build or application components.

**Version-specific limitation:** WAL documentation §11 reports a race involving concurrent writes/checkpoints, fixed in 3.51.3 and later, with listed backports 3.44.6 and 3.50.7. Require a fixed linked runtime before enabling concurrent WAL connections; record vendor patch evidence when relying on a backport. This is a documented fixed issue, not a reproduced failure or a claim that every SQLite build is affected.

### Ledger evidence categories and gaps

- **Inspected source:** the precise TigerBeetle/Formance files and licenses above, retrieved without execution.
- **Documented guarantee:** database/driver behavior within documented assumptions.
- **Practitioner report:** maintainer PR, TigerBeetle blog and Stripe article, not independently reproduced incidents.
- **Design hypothesis:** protected posting, row locks, synthetic closure, partitioning and resource limits fit this project.
- **Proposed experiment:** every local test and benchmark in the blueprint; no passing result exists.
- **Unresolved:** current Formance v3 reuse rights and complete transitive runtime compatibility/license audit. Source inspection does not establish an SLA, deployment security or production fitness.

## Chronicle temporal systems and operational lessons

Checked 2026-10-23. All code observations below are source inspection, not executed reproduction. GitHub public read APIs returned the referenced source and revision metadata. No upstream package was installed. Proposed project behavior is a design recommendation.

<a id="c-01"></a>
### C-01 — Feast: known-time cutoff and preservation of unmatched rows

- Repository: [feast-dev/feast](https://github.com/feast-dev/feast), inspected commit [`e304a84f979b0f3b828b7c778618d7b7eaa6a2d2`](https://github.com/feast-dev/feast/commit/e304a84f979b0f3b828b7c778618d7b7eaa6a2d2), commit metadata dated 2026-10-19. The [v0.66.0 release](https://github.com/feast-dev/feast/releases/tag/v0.66.0) lists the created-timestamp feature; the inspected October commit is not claimed to equal that release.
- Inspected [PostgreSQL implementation](https://github.com/feast-dev/feast/blob/e304a84f979b0f3b828b7c778618d7b7eaa6a2d2/sdk/python/feast/infra/offline_stores/contrib/postgres_offline_store/postgres.py#L1004-L1020): the opt-in predicate requires created time no later than entity time. Default event-time selection alone can admit a later correction.
- Inspected [tests](https://github.com/feast-dev/feast/blob/e304a84f979b0f3b828b7c778618d7b7eaa6a2d2/sdk/python/tests/unit/infra/offline_stores/test_filter_by_created_timestamp.py): template gating (lines 65–109), old/new corrected values (197–205), NULL created time (207–221), all-future candidates and entity isolation (223–280). File and Ibis cases exercise behavior; template tests alone do not execute PostgreSQL.
- [PR #6617](https://github.com/feast-dev/feast/pull/6617), merged as [`79b33ce4329ef29322dddde18a0877d75e1039c8`](https://github.com/feast-dev/feast/commit/79b33ce4329ef29322dddde18a0877d75e1039c8), contains a July 29 review identifying left-join rows disappearing after post-join filtering. The July 31 author reply says unmatched entity rows were restored; inspected tests now assert that behavior. This is a reviewed implementation defect, not evidence of a continuing released defect or customer incident.
- Transfer: filter available versions before choosing a winner; preserve each requested observation with an explicit missing reason. Do not transfer an online-availability claim unless the timestamp actually means online availability. Chronicle adds source evidence and actual snapshot membership.
- Local falsification: a request with only future-created candidates must survive with `NOT_AVAILABLE_AT_CUTOFF`; remove the availability predicate and require the leakage test to fail.
- [Inspected LICENSE](https://github.com/feast-dev/feast/blob/e304a84f979b0f3b828b7c778618d7b7eaa6a2d2/LICENSE): Apache-2.0. Proposed use: design reference and optional comparison baseline, no dependency or copied implementation. If adapting code, retain license/copyright/notices, mark modifications, and inspect any applicable NOTICE file and third-party material. No legal approval inferred.

<a id="c-02"></a>
### C-02 — XTDB: temporal resolution and independent test anchors

- Inspected release v2.2.0, tag object `e24cf80b10d6f19384d9e141ebd84f70be68268a`, pointing to commit [`c6c19623d90c658c15400fb46dc056f3735ed57b`](https://github.com/xtdb/xtdb/commit/c6c19623d90c658c15400fb46dc056f3735ed57b). Tag metadata: 2026-10-21. This is XTDB 2.x; do not import 1.x architecture assumptions.
- Inspected [EntityResolver.kt](https://github.com/xtdb/xtdb/blob/c6c19623d90c658c15400fb46dc056f3735ed57b/core/src/main/kotlin/xtdb/operator/scan/EntityResolver.kt): `AsOfResolver` checks system-time bound and half-open valid-time membership; erase has special semantics. Chronicle's withdrawal is a versioned absence, not XTDB erasure.
- Inspected [AsOfResolverTest.kt](https://github.com/xtdb/xtdb/blob/c6c19623d90c658c15400fb46dc056f3735ed57b/core/src/test/kotlin/xtdb/operator/scan/AsOfResolverTest.kt): concrete clipping anchors (204–215) accompany 500-case comparison between optimized and polygon resolvers (219–225). Source inspection does not confirm this suite passes in this environment.
- Transfer: combine frozen expected answers with differential tests; two agreeing implementations can share an error or both return nothing. Do not copy its storage kernel or claim its temporal/distributed guarantees for Chronicle.
- Local falsification: mutate availability boundary from exclusive end to inclusive end; use a hand-calculated cutoff trace that must fail, alongside SQL-vs-scan equivalence.
- [LICENSE](https://github.com/xtdb/xtdb/blob/c6c19623d90c658c15400fb46dc056f3735ed57b/LICENSE), inspected: MPL-2.0. Proposed use: design reference; optional future engine spike. Distribution of modified covered files carries source/notice obligations; separate files in a larger work are a different question. No code copying proposed.

<a id="c-03"></a>
### C-03 — XTDB operational lesson: cancellation must reach execution

- [v2.2.0-rc2 release notes](https://github.com/xtdb/xtdb/releases/tag/v2.2.0-rc2), published 2026-10-20, section “Things that clean up after themselves,” describe disconnect and cancellation fixes [#5930](https://github.com/xtdb/xtdb/pull/5930) and [#5929](https://github.com/xtdb/xtdb/pull/5929). The maintainer explains that a blocking aggregate could keep computing because cancellation was checked only at the output root. This is a maintainer release account, not an independently reproduced incident; the notes describe a fix, not a current defect.
- Transfer: HTTP cancellation alone is insufficient. Chronicle cancels the database connection/query, bounds statement execution, supervises report subprocesses, and tests that resources actually stop. Do not transfer the upstream speedup numbers.
- Local falsification: abandon a request while the query is blocked; assert PostgreSQL no longer executes that job after the configured cancellation budget. A mock raising `CancelledError` cannot establish this.

<a id="c-04"></a>
### C-04 — dbt snapshots: observed history is not complete historical knowledge

- Canonical redirect observed: `dbt-labs/dbt-core` → [`dbt-labs/dbt`](https://github.com/dbt-labs/dbt). Current default branch has a Rust/v2 layout. Historical reference inspected at v1.10.0 commit [`ca1ac9dc9f6be41b133e33c49d24637246b75bff`](https://github.com/dbt-labs/dbt/tree/ca1ac9dc9f6be41b133e33c49d24637246b75bff). No claim that 1.10 is the currently recommended installation.
- [Official snapshots documentation](https://docs.getdbt.com/docs/build/snapshots), “What are snapshots?”: type-2 history over changing source tables. Inspected [hard-delete tests](https://github.com/dbt-labs/dbt/blob/ca1ac9dc9f6be41b133e33c49d24637246b75bff/tests/functional/snapshots/test_hard_delete_snapshot.py) and [timestamp-configuration test](https://github.com/dbt-labs/dbt/blob/ca1ac9dc9f6be41b133e33c49d24637246b75bff/tests/functional/snapshots/test_snapshot_timestamps.py). The former checks invalidation around snapshot execution and revival using source updated time. Adapter materialization macros were not audited.
- Transfer: preserve successive observed source states and explicitly model deletion. A scheduled snapshot cannot recover a source version overwritten between observations; nor does its timestamp establish original public availability.
- Local falsification: change a source twice between snapshot runs and verify that the missing intermediate version is reported unavailable, not invented.
- [License.md](https://github.com/dbt-labs/dbt/blob/ca1ac9dc9f6be41b133e33c49d24637246b75bff/License.md), inspected: Apache-2.0. Use: design reference; dbt + artifact versioning remains a sensible simpler solution for ordinary warehouse history. Chronicle owns the explicit source-availability, snapshot, evidence, and recomputation contracts; no dbt dependency initially. Any later adoption requires version-specific adapter and license review.

<a id="c-05"></a>
### C-05 — Storage/metadata restore mismatch

- [XTDB official restore runbook](https://docs.xtdb.com/ops/backup-and-restore/out-of-sync-log.html), current documentation inspected 2026-10-23, says a log behind indexed storage prevents startup; a new epoch is an explicit recovery choice and cannot reconstruct lost unindexed transactions.
- Transfer: Chronicle verifies the complete referenced object closure before opening a restored snapshot; missing objects do not become “empty datasets.” This is an analogy in failure reasoning, not the same storage design. Proposed experiment: restore PostgreSQL metadata newer than the available object backup and require a blocked/degraded snapshot state with exact missing object IDs.

### Chronicle source limitations

Selected deep inspections cover temporal selection, concrete/differential testing, and an actual maintainer review/fix. No repository code was run, no benchmark reproduced, no package compatibility matrix executed, and no complete upstream security audit performed. Current documentation can move; the repository links above are pinned. The shared entries provide common runtime dependency licenses and separate FRED/BLS/Board rights findings. DVC, lakeFS and Dolt are alternatives for consideration, not implied installed dependencies.

## Data rights, real archive evidence and AI evaluation

Observed 2026-10-23. Public primary pages, repository source and maintainer records were inspected. No model call, upstream test, API-key use or registration occurred.

<a id="d-01"></a>
### D-01 — FRED current terms and the older API page

- Primary: https://fred.stlouisfed.org/legal/ — **Full FRED Services Terms**, **Additional Terms of Use for the FRED API**, specifically prohibitions (k), (l), (m). Current page read 2026-10-23 and verified by a second search engine.
- Contrast: https://fred.stlouisfed.org/docs/api/terms_of_use.html — older-format API page read the same day; omits the newer prohibitions list. No favorable inference is made from that omission.
- Supported observation: current language restricts AI-related uses and API storage/cache/archive/database incorporation. The user has supplied no permission resolving those restrictions for Chronicle.
- Decision: **disabled candidate adapter**, pending a compatible documented rights basis. Public-domain source tags do not by themselves establish this basis.
- Transfer: rights are acquisition- and use-specific; model-input/export/persistence checks should be separate.
- Non-transfer: no general legal opinion, no claim that all economic data are restricted, no authorized registration or outreach.
- Local falsification: an input marked `persistent_ingest=false` or `model_input=false` must be rejected at the relevant sink, including derived mixed-input results.

<a id="d-02"></a>
### D-02 — Dated confirmation of the FRED policy change and key access

- Dated official notice: https://fred.stlouisfed.org/news/2024/06/weve-updated-our-terms-of-use-action-requested — **2024-06-18**, changes list. Read 2026-10-23.
- API key documentation: https://fred.stlouisfed.org/docs/api/api_key.html — **API Keys, FRED API Version 1**, requirements. Read 2026-10-23.
- Supported observation: the restriction is not merely inferred from a generic warning; the dated notice names AI and API archiving changes. V1 requests require a registered key; docs state application users should use their own key.
- Decision: source reference only; no API request or key use performed. If later enabled, use a separately obtained user key and review current terms again.
- Non-transfer: an example key is not usable authorization. A no-cache transient path cannot make a reproducible archive and should not be represented as one.
- Local falsification: offline clean-start must succeed with no `FRED_API_KEY`; disabled connector must make no network call.

<a id="d-03"></a>
### D-03 — Candidate series metadata and historical-unit evidence

- https://fred.stlouisfed.org/series/PAYEMS — **Notes**, source code `CES0000000001`, units/frequency/seasonal adjustment.
- https://fred.stlouisfed.org/series/INDPRO — **Notes**, current units/frequency/seasonal adjustment.
- https://alfred.stlouisfed.org/series?seid=PAYEMS — **Notes / Release Dates**.
- https://alfred.stlouisfed.org/series?seid=INDPRO — **Units / Release Dates**.
- Supported: monthly seasonally adjusted payroll employment uses thousands of persons; current production index uses 2017=100. ALFRED records INDPRO unit changes, including the 2021-05-28 move from 2012 to 2017 base. The pages carry public-domain/citation tags while also containing generic graph copyright boilerplate; do not treat a boilerplate string as a series-specific license grant.
- Evidence boundary: inspected metadata, **not** complete observation-vintage enumeration or verified API response coverage. Old source release dates are not historical local ingestion times. No guarantee of intraday availability.
- Transfer: explicit unit-version identity and source-time precision.
- Local falsification: mixing base-2012 and base-2017 values without an approved transform must fail or produce an incompatibility result, not a smooth chart.

<a id="d-04"></a>
### D-04 — Direct BLS publication rights

- https://www.bls.gov/bls/linksite.htm — **Copyright**, last-modified date shown 2016-08-05; read 2026-10-23.
- Supported: BLS identifies its publications as public domain apart from previously copyrighted photos/illustrations, requests attribution, and separately protects the emblem.
- Decision: direct numerical tables are the recommended real-data source after recording per-artifact policy and exceptions. No photos/illustrations/logos are included in the proposed corpus.
- Non-transfer: does not grant rights to FRED-acquired copies under separate service terms or to external linked material.
- Local falsification: a parser's source registry rejects unknown domains/content classes even if the table title looks like BLS.

<a id="d-05"></a>
### D-05 — Direct Federal Reserve Board publication rights

- https://www.federalreserve.gov/disclaimer.htm — **Copyright/trademark**, **Seals/Logos**, **Disclaimer regarding Non-Board Information**; read 2026-10-23.
- Supported: Board information is generally public domain unless indicated otherwise; copying/distribution with source citation is allowed, third-party exceptions and insignia are separate.
- Decision: recommended direct-source numerical-table corpus with per-document review; not a blanket license to every linked source.
- Local falsification: a permission record for numerical tables must not authorize embedded non-Board images or the official seal.

<a id="d-06"></a>
### D-06 — Two real BLS vintages with an independent published check

- https://www.bls.gov/news.release/archives/empsit_02022024.htm — release header and **Table B-1, Total nonfarm, Seasonally adjusted**, plus Summary Table B; published 2024-02-02.
- https://www.bls.gov/news.release/archives/empsit_03082024.htm — same table and **revision paragraph**; published 2024-03-08.
- Supported cells: first Dec/Jan levels 157347/157700; revised Dec/Jan 157304/157533, in thousands. Subtractions yield January change 353 and 229. The March narrative independently states 353,000 revised to 229,000, supporting a second manual check against the table extraction.
- Acquisition: direct publisher, separate from ALFRED. BLS publication rights from D-04; no source code license applies to this numerical corpus.
- Transfer: real revisions, adjacent unadjusted-column trap, publication versus actual receipt distinction. Date/time printed on a release is a publisher assertion, not network-availability measurement.
- Non-transfer: two archives do not establish complete historical coverage or a general HTML parser.
- Local falsification: change parser to use unadjusted columns; the manually frozen expected levels and narrative delta must reject it.

<a id="d-07"></a>
### D-07 — Two direct Board production vintages

- https://www.federalreserve.gov/releases/g17/20240215/ — **Industrial Production and Capacity Utilization: Summary**, total-index row; published 2024-02-15.
- https://www.federalreserve.gov/releases/g17/20240315/ — same row; published 2024-03-15.
- Supported cells: first Nov/Dec/Jan 102.7/102.7/102.6; revised 103.0/102.7/102.2, index 2017=100. Summary precision is one decimal. Distinguish total-index values from “Previous estimates.”
- Decision: direct real corpus; publisher rights from D-05. Preserve table-level precision and source identity instead of silently merging with FRED INDPRO's more granular series.
- Transfer: revisions affecting a window through more than one input; immutable source release plus local import timestamps.
- Non-transfer: no all-history vintage coverage, no inference of intraday release availability from a date-only page.
- Local falsification: ignore the November revision or select the “Previous estimates” row; full-recompute oracle must disagree with incremental/parsed result.

<a id="a-01"></a>
### A-01 — Inspect AI numeric scorer and tests: use as a reference, not an oracle

Repository: https://github.com/UKGovernmentBEIS/inspect_ai . Canonical repository observed unchanged. Inspected revision **`80118ca5a96d557bbd2a778d0a032199906b53c4`**, commit dated 2026-10-23. The releases endpoint showed **0.3.283**, published 2026-10-21; current commit is a source-reading pin, **not** a chosen dependency version. Recent commit/release is maintenance evidence, not a support guarantee.

Inspected immutable locations:

- [LICENSE](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/LICENSE): **MIT**, copyright UK AI Security Institute. If substantial code is reused, retain copyright and permission notice; no code copied here.
- [src/inspect_ai/scorer/_match.py](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/src/inspect_ai/scorer/_match.py), `match`.
- [src/inspect_ai/scorer/_common.py](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/src/inspect_ai/scorer/_common.py), `match_str`, `_parse_number`, `normalize_number`.
- [tests/scorer/test_match.py](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/tests/scorer/test_match.py): `test_numeric_end_no_digit_suffix_match`, `test_numeric_negative_target_wrong_sign`, `test_numeric_match_operator_prefix_stays_incorrect`.
- [fix commit 660c482516ac3fafbed8cfb856ecb3a6e8add7f4](https://github.com/UKGovernmentBEIS/inspect_ai/commit/660c482516ac3fafbed8cfb856ecb3a6e8add7f4), dated 2026-05-12; diff and added tests read.

**Mechanism and operational lesson:** the historical numeric path could fall into substring matching, or mishandle signed numeric targets, causing false correct grades. The inspected fix compares normalized numeric values and adds negative cases. The inspected current generic normalizer still uses floating conversion and five significant digits: it is **not** the Chronicle decimal-12/unit oracle.

Proposed use: **design reference; optional later evaluation adapter**, deliberately excluded from the initial runtime. Transfer adversarial grader testing and explicit numeric semantics; do not inherit quantitative-evidence correctness from a framework. Local falsification: 25 vs 5, +5 vs -5, one-unit-scale errors and close-but-unequal decimal-12 outputs must fail. No historical bug reproduction or test execution was performed; no earliest fixed release is asserted.

<a id="a-02"></a>
### A-02 — Maintainer-reviewed grader model reuse failure

- [PR #5570](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5570), merged **2026-09-29**, merge commit **`8fdc4f6be524ddb02aaec85e723a434884f5c837`**; metadata, rationale and validation limitations read.
- At the A-01 inspected revision: [src/inspect_ai/scorer/_model.py](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/src/inspect_ai/scorer/_model.py), `_model_graded_qa_single` resolves active model per call.
- [tests/scorer/test_model_graded.py](https://github.com/UKGovernmentBEIS/inspect_ai/blob/80118ca5a96d557bbd2a778d0a032199906b53c4/tests/scorer/test_model_graded.py), `test_model_graded_scorer_instance_grades_with_each_active_model` and `test_model_graded_scorer_instance_resolves_role_on_each_call`.
- Same repository/revision/license/use as A-01; no separate dependency.

**Failure → cause → fix → assumptions:** reusing one scorer under multiple active models could keep the first resolved model in a nonlocal closure, so later trials were graded by the wrong identity. Per-call resolution and behavioral regression tests replace that state retention. The PR reports mutant checks showing that moving the cache into a list evades closure-shape inspection but still fails the behavioral test. Scope includes the explicitly identified no-role scorer path; the report notes default grader-role behavior was not affected. Upstream reported testing is not our testing.

**Transfer:** trial/model/cache identity must be explicit and tested through observable outputs. **Does not transfer:** tests with mock model contexts do not establish real-model answer quality or end-to-end provider behavior. The inspected revision contains the fix; this is not a claim of a current unfixed issue. **Local falsification:** one evaluator object grades under mock A then B, with different expected outputs; B must not receive A's cached output. Include prompt/policy changes in this regression.

<a id="a-03"></a>
### A-03 — First-person evaluation practice, scope-limited

- https://www.anthropic.com/research/demystifying-evals-for-ai-agents — **2026-01-09**, sections **The structure of an evaluation**, **Build balanced problem sets**, **Write unambiguous tasks with reference solutions**.
- First-person lesson: the team describes balancing web-search under-triggering and over-triggering using cases requiring search and cases that should not search. Transfer: Chronicle needs both correct abstention and correct non-abstention cases; an always-refuse policy is not useful success.
- Distinguish agent-reported success from observable outcome. In Chronicle, the published claim must match the retained deterministic result/evidence, not the assistant's confidence.
- Evidence type: dated practitioner account, not an independently reproduced experiment or neutral comparative benchmark; broader productivity claims are not adopted.
- Use: **design reference only**, no code copied and no license for article text inferred.
- Local falsification: pair an unanswerable cutoff question with an answerable one; the same refusal on both must fail usefulness evaluation while successful unsupported claims fail grounding evaluation.

## Shared runtime, alternatives and license observations

Checked 2026-10-23 by reading public primary documentation, GitHub source files, license files and release/commit metadata. Repository code was not executed. A version or recent commit is maintenance evidence, not a support promise, security audit or compatibility test.

<a id="r-01"></a>
### R-01 — DVC: artifact identity and dependency planning

- Canonical redirect verified: `iterative/dvc` now resolves to [treeverse/dvc](https://github.com/treeverse/dvc). Inspected commit [`56e59829512ff134aa269099a2099587b810b4dd`](https://github.com/treeverse/dvc/commit/56e59829512ff134aa269099a2099587b810b4dd), dated 2026-08-06. [3.67.1 release](https://github.com/treeverse/dvc/releases/tag/3.67.1) was published 2026-03-31; the inspected later commit is not claimed to be that release.
- Actual code: [`dvc/repo/reproduce.py`](https://github.com/treeverse/dvc/blob/56e59829512ff134aa269099a2099587b810b4dd/dvc/repo/reproduce.py), `get_active_graph`, `plan_repro`, `handle_error`; actual tests: [`tests/unit/repo/test_reproduce.py`](https://github.com/treeverse/dvc/blob/56e59829512ff134aa269099a2099587b810b4dd/tests/unit/repo/test_reproduce.py), `test_active_graph`, `test_repro_plan`, `test_number_reproduces`.
- Observed mechanism: graph planning uses postorder traversal; frozen stages change dependency traversal; the test includes upstream and downstream plans. Transfer the requirement to name dependency direction and validate affected outputs. This is stage-level planning, not automatic inference of revised monthly windows or original data availability.
- Proposed use: optional artifact-reproduction baseline; deliberately excluded as a mandatory service or initial runtime dependency. A small dataset/model experiment may sensibly use DVC directly. No substantive code adaptation proposed.
- Local falsification: revise month M, compare the declared affected report set with an independent full recalculation; delete an edge and require the mutation to be caught. Separately remove a cached object and require historical replay to report missing bytes rather than “success.”
- [LICENSE at inspected commit](https://github.com/treeverse/dvc/blob/56e59829512ff134aa269099a2099587b810b4dd/LICENSE): Apache-2.0. For code redistribution, keep license/copyright/notices, identify modifications and inspect applicable NOTICE/third-party files. The observed copyright mentions Treeverse; do not assume ownership or current canonical paths from older tutorials.

<a id="r-02"></a>
### R-02 — Dolt: useful snapshot baseline, separate data and definition history

- Inspected repository metadata and [LICENSE](https://github.com/dolthub/dolt/blob/05e6bceb1a210a7a8d770b74b8ef4825c165c0a8/LICENSE) at commit [`05e6bceb1a210a7a8d770b74b8ef4825c165c0a8`](https://github.com/dolthub/dolt/commit/05e6bceb1a210a7a8d770b74b8ef4825c165c0a8), tagged/released [v2.6.0](https://github.com/dolthub/dolt/releases/tag/v2.6.0), 2026-10-23. License Apache-2.0; retain license/notices and changed-file attribution if adapted. Use: comparison/design reference, excluded from initial runtime. No Dolt source implementation or integration test was successfully inspected in depth; an attempted guessed test path returned 404 and supports no claim.
- Primary [querying-history documentation](https://www.dolthub.com/docs/sql-reference/version-control/querying-history/), checked 2026-10-23: snapshots are Dolt commits, not every SQL transaction commit. The view-history section distinguishes old data under a current view definition from a commit-qualified historical definition.
- Transfer: a data commit alone does not identify the transform used for an old result. Chronicle explicitly pins both. Do not transfer Dolt merge/versioning capabilities into a hand-built PostgreSQL schema, or confuse Dolt commit time with original public release time.
- Proposed local falsification: alter a report's formula without changing its data; the manifest must make the changed definition visible and the four-way comparison must distinguish it. Optional future spike: repeat against a pinned Dolt commit as a baseline, with engine/version semantics recorded.

<a id="r-03"></a>
### R-03 — lakeFS: repository availability does not imply a permissive license

- Inspected [README](https://github.com/treeverse/lakeFS/blob/34567699eab33d1bc9763b163d301ed06b1e21d8/README.md) and [LICENSE](https://github.com/treeverse/lakeFS/blob/34567699eab33d1bc9763b163d301ed06b1e21d8/LICENSE) at commit [`34567699eab33d1bc9763b163d301ed06b1e21d8`](https://github.com/treeverse/lakeFS/commit/34567699eab33d1bc9763b163d301ed06b1e21d8), dated 2026-10-07. Release metadata lists [v1.88.0](https://github.com/treeverse/lakeFS/releases/tag/v1.88.0), 2026-10-01. The inspected license's Licensed Work parameter says **lakeFS 1.87.0**; this mismatch is recorded, not silently resolved.
- License observed: **Business Source License 1.1**, with a future Apache-2.0 change license and a version-relative change date. Its additional production grant is limited; the text distinguishes unmodified internal use from exposing functionality to third parties. This is source-available, not evidence that this current revision is Apache-licensed. Full applicability requires version/use-specific review.
- README describes object-store branching and write-audit-publish; these are project descriptions, not reproduced performance or guarantees. Proposed use: conceptual alternative, no dependency, no copied code. Exclusion is based on unnecessary operational scope plus unresolved deployment/license fit; it is not a claim that lakeFS is technically incapable.
- Local falsification for our own design: fail after object write but before metadata publication. Readers must remain on the old complete snapshot. Do not claim lakeFS internals were audited or that this small protocol matches its implementation.

<a id="r-04"></a>
### R-04 — PostgreSQL 18: transactions, privileged routines and backup boundaries

- Official versioned references inspected: [transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html), [CREATE FUNCTION](https://www.postgresql.org/docs/18/sql-createfunction.html), [SQL dump](https://www.postgresql.org/docs/18/backup-dump.html), [continuous archiving](https://www.postgresql.org/docs/18/continuous-archiving.html). The retrieved `/current/` pages identified themselves as PostgreSQL 18; these links fix the intended major version.
- Documented, not reproduced here: READ COMMITTED has statement-level snapshots and update conflict behavior; a later relative/conditional update is not the same operation as an application writing a stale absolute value. `SECURITY DEFINER` requires care over search path and execution privilege. A logical dump is an internally consistent database snapshot, not a WAL/PITR base backup and not a snapshot of an independent provider or external object store.
- Transfer: choose explicit transaction boundaries; revoke direct financial DML from API roles; use locked guard rows and deferred consistency checks; preserve complete external evidence separately during restore. Do not inherit serializable, exactly-once, multi-store atomicity or media-failure guarantees merely because PostgreSQL is present.
- Local falsification: three distinct SQL debit sequences; a spoofed search-path object under the least-privileged API role; restore a pre-payout dump while provider state is newer; restore metadata referencing absent object files. Required expected outcomes are in each verification document.
- [REL_18_0 COPYRIGHT](https://github.com/postgres/postgres/blob/REL_18_0/COPYRIGHT) inspected: PostgreSQL license. Use as unmodified database dependency. Exact supported patch, image digest, extensions and platform package notices are selected and tested at bootstrap; 18.0 is a license inspection anchor, not advice to install an old patch.

<a id="r-05"></a>
### R-05 — psycopg: implicit transactions and error boundaries

- Inspected release tag **3.3.6**, annotated tag `805587083d74fc73a51aa6c63bde9e81cf103ad4`, pointing to commit [`a67654d1e7afbf9b3a619557838f62de1c790e7c`](https://github.com/psycopg/psycopg/commit/a67654d1e7afbf9b3a619557838f62de1c790e7c), tag date 2026-09-18.
- [Transaction documentation](https://www.psycopg.org/psycopg3/docs/basic/transactions.html), checked 2026-10-23, currently renders development documentation; use it only for the behavior corroborated by pinned source tests. [`tests/test_transaction.py`](https://github.com/psycopg/psycopg/blob/a67654d1e7afbf9b3a619557838f62de1c790e7c/tests/test_transaction.py), inspected tests `test_basic`, `test_commit_on_successful_exit`, `test_rollback_on_exception_exit`, `test_context_inerror_rollback_no_clobber`, includes backend termination. The synchronous file is generated from its async counterpart; no edit/reuse proposed.
- Documented/observed: a simple query can open an implicit transaction; transaction contexts and exception handling affect commit/rollback. Recommended use: autocommit connections with explicitly entered transaction scopes for business writes, subject to tests; rollback/discard failed connections before reuse. A transport failure around COMMIT still needs a durable operation lookup, not an assumed rollback.
- Local falsification: kill the connection at commit/reply boundaries, reuse a pool connection after an exception, and inspect actual transaction state from a separate session. These project tests have not run.
- [LICENSE.txt](https://github.com/psycopg/psycopg/blob/a67654d1e7afbf9b3a619557838f62de1c790e7c/LICENSE.txt): LGPL-3.0. Proposed unmodified dependency; preserve license/source and relinking obligations appropriate to the distribution. Bundled binary wheels/libpq/OpenSSL and modified driver files require their own notices/review. Do not label it MIT or treat license inspection as legal clearance.

<a id="u-01"></a>
### U-01 — Runtime dependency candidates and lock boundary

The table records **source/license observations**, not a working lockfile. Release tags are version evidence; commits are used for deeply inspected mechanisms. The first implementation PR resolves current supported patch builds and exact transitive locks/image digests, checks notices and executes compatibility tests. No packages were installed for this blueprint. Source inspection does not establish absence of vulnerabilities.

| Component | Inspected candidate or license anchor | License observed / intended role |
|---|---|---|
| CPython | [v3.13.9 LICENSE](https://github.com/python/cpython/blob/v3.13.9/LICENSE), sections A/B read | PSF-2.0 with separately licensed incorporated material; Python 3.13 family runtime, latest suitable supported patch to be locked |
| PostgreSQL | [REL_18_0 COPYRIGHT](https://github.com/postgres/postgres/blob/REL_18_0/COPYRIGHT) | PostgreSQL license; PostgreSQL 18 family, supported patch/image digest to be locked |
| FastAPI | [0.161.1 release](https://github.com/fastapi/fastapi/releases/tag/0.161.1), 2026-10-22; [LICENSE at 69ddc2badf9da1123bba4d360225ae6b085985f6](https://github.com/fastapi/fastapi/blob/69ddc2badf9da1123bba4d360225ae6b085985f6/LICENSE) | MIT; HTTP validation/routing dependency, not transaction authority |
| Pydantic | [v2.14.1 release](https://github.com/pydantic/pydantic/releases/tag/v2.14.1), 2026-10-11; [LICENSE](https://github.com/pydantic/pydantic/blob/v2.14.1/LICENSE) | MIT; strict typed boundaries; pydantic-core and transitive notices remain a lock review item |
| psycopg | 3.3.6, commit and license in R-05 | LGPL-3.0; explicit PostgreSQL client, not source copied into application |
| Alembic | [rel_1_20_1 release](https://github.com/sqlalchemy/alembic/releases/tag/rel_1_20_1), 2026-10-22; [LICENSE](https://github.com/sqlalchemy/alembic/blob/rel_1_20_1/LICENSE) | MIT; revision runner with explicit SQL; SQLAlchemy transitively required, not a mandate for ORM posting logic |
| pytest | [9.1.1 release](https://github.com/pytest-dev/pytest/releases/tag/9.1.1), 2026-06-19; [LICENSE](https://github.com/pytest-dev/pytest/blob/9.1.1/LICENSE) | MIT; tests only |
| Hypothesis | [v6.170.8 release](https://github.com/HypothesisWorks/hypothesis/releases/tag/v6.170.8), 2026-10-22; [LICENSE.txt](https://github.com/HypothesisWorks/hypothesis/blob/v6.170.8/LICENSE.txt) | **MPL-2.0** except noted material at this revision; testing dependency. Do not inherit older licensing assumptions |
| React | Canonical `facebook/react` → [react/react](https://github.com/react/react); [v19.3.1 release](https://github.com/react/react/releases/tag/v19.3.1), 2026-10-14; [LICENSE](https://github.com/react/react/blob/v19.3.1/LICENSE) | MIT; deferred UI candidate; no Node/build-chain lock or compatibility test performed |

MIT redistribution keeps copyright and permission notices. Apache-2.0 adds license/notice and change marking requirements. MPL-2.0 concerns covered files and source availability on distribution; LGPL has library/combined-work conditions. These are orientation notes, not a complete distribution assessment. There is no substantial upstream code in this blueprint bundle.

**Bootstrap inventory still required:** SQLAlchemy/Alembic transitive graph; `psycopg_pool` if selected; ASGI server and HTTP client; Docker/Podman/Compose host tooling and image layers; frontend TypeScript/build/test dependencies; any model SDK. These are not silently licensed by the table above. Prefer an unmodified standard dependency over vendoring. An offline synthetic R1 need not select a model, GPU library, cloud SDK or agent framework. Resolve the smallest required dependency graph for that release, save exact locks and `THIRD_PARTY_NOTICES`, and keep deferred dependencies out until their release.

### Shared interpretation boundary

Research establishes that the linked code/docs/license text were inspectable at the recorded revisions. It does not certify either project's future implementation. No adoption, throughput, recovery time, model accuracy or learner mastery has been observed. Comparisons with mature tools are design judgments informed by these sources, to be challenged with the proposed experiments.
