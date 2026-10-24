# LedgerOps and Chronicle — engineering review package

**Prepared and source-checked:** 2026-10-23. **State:** design only. **Decision authority:** Yi Yang; architecture review by Claude/Codex as delegated. This package supplies recommendations for review, not a record that either reviewer or the user has approved them.

## 1. Recommendation

Build both selected projects as independent systems. LedgerOps owns synthetic financial execution, uncertainty and operational reconciliation. Chronicle owns historical information selection, reproducible analysis and the principal read-only AI application. Preserve the ambitious release scope, while making each release a coherent, independently inspectable increment.

Use a deliberately understandable implementation substrate: Python, explicit PostgreSQL transactions and SQL, strict typed boundaries, real-component tests and a small UI when there is an actual investigation workflow to expose. The depth comes from the contracts and hostile tests, not service count. Neither a distributed database kernel nor a generic workflow platform needs to be reimplemented.

| Project | Recommended initial architecture | Owned engineering contribution |
|---|---|---|
| LedgerOps | FastAPI application and explicit psycopg transactions over PostgreSQL 18; worker added for external intents; separate synthetic provider process with independent SQLite WAL store; narrow operator UI later | Atomic balanced postings and projections; operation-level replay; hold versus remote-outcome state; independent evidence reconciliation; fenced publication and version-bound approval; restore without blind redispatch |
| Chronicle | PostgreSQL 18 for normalized immutable revisions, metadata, queries and snapshot membership; immutable local files for raw source evidence and report bundles; deterministic report API before AI; narrow React workflow later | Source availability versus local knowledge; atomic snapshot publication across metadata/files; precise historical selection; revision-aware recalculation; data/code change attribution; externally enforced AI evidence policy |

These are **provisional defaults**. [Dependency candidates and license evidence](01-REFERENCE-REGISTER.md#u-01) are source-audited but not installed or compatibility-tested. The first implementation PR must produce the actual lockfiles, image digests and clean-start result.

## 2. Material findings that changed the design

### 2.1 A public series label is insufficient for Chronicle's intended data use

Current FRED/ALFRED terms and API conditions require a materially different sourcing plan from the original assumption. See the concise source-specific analysis in [DATA-RIGHTS-AND-AI-EVAL](chronicle/DATA-RIGHTS-AND-AI-EVAL.md) and the D-series entries in the [reference register](01-REFERENCE-REGISTER.md). Do not put an enabled persistent FRED ingestion path or FRED-based AI evaluation into C1.

**Design response:** the complete workflow starts on owned synthetic fixtures. A narrow, separately sourced historical example uses original BLS and Federal Reserve Board release archives after the documented per-source checks. The four-document candidate corpus genuinely contains revised historical values; it is not a claim of complete ALFRED coverage. FRED/ALFRED remains a valuable semantic reference and a disabled adapter candidate pending compatible permission/clarification. A link-only or genuinely ephemeral permitted retrieval path is distinct from an archive, export or AI path. No account registration, source permissions request or live API-key use was performed.

### 2.2 Pending funds and unknown external outcome are independent

A payout may have a live hold while the system has no conclusive knowledge of its provider outcome. A timeout, HTTP 500 or lagging `NOT_FOUND` response cannot authorize releasing the hold or creating another payment identity. The proposed simulator has an explicit atomic **close-if-absent** operation: it either returns an existing execution or persists an irrevocable no-execution tombstone. This makes a negative-finality contract falsifiable. It is a designed simulator capability, not a claim that all financial providers offer it. See [LedgerOps contracts](ledgerops/CONTRACTS.md) and L-series references.

### 2.3 Version history is more than saving `updated_at`

Chronicle records effective period, source availability with precision/evidence, actual local receipt and explicit committed snapshot membership. It cannot claim that a new system stored a revision years before it existed. A source-as-of reconstruction made today and the system's actual past snapshot are different products. The read API names that distinction. The inspected Feast code already includes an optional created-time filter; this package does not invent a missing capability to justify Chronicle. See [C-01](01-REFERENCE-REGISTER.md#c-01).

### 2.4 Repository names, architecture and licenses have changed

The register distinguishes Formance's inspected PostgreSQL v2 code from its new main-branch architecture; historical dbt 1.x tests from the current canonical repository; and DVC's canonical repository location. The inspected lakeFS revision uses BUSL-1.1, with a version-label mismatch recorded explicitly. The runtime table also records actual license text, including psycopg LGPL and the inspected Hypothesis MPL license. No dependency is selected because an old blog called it permissive. These findings narrow reuse decisions; they do not require adding replacement frameworks.

### 2.5 The independent oracle is an architecture component

LedgerOps does not use its own journal export as the external truth. Chronicle does not use its production selection/recalculation functions to create its expected answers. Hand-calculated fixtures, separately written small reference models, independent input provenance and deliberate defects complement one another. Two AI-written implementations agreeing with each other can still share a specification error. The verification plans therefore include a human review of the smallest accounting/time examples and mutation tests that must fail for the right reason.

## 3. What to build, buy or deliberately leave out

**Use mature components** for storage durability, HTTP handling, SQL execution, schema validation, migration bookkeeping, test runners and property generation. Use the database's locking and constraint machinery rather than simulating transactions in Python. Reuse upstream libraries without copying their internal code when a dependency suffices.

**Own LedgerOps's business contract**: which accounts a command affects, what balance is available, how requests map to durable operations, what evidence permits terminal state, how mismatches are identified and how stale decisions are rejected. A mature ledger could be the right production choice. Wrapping one would hide too much of the specific transaction reasoning this portfolio intends to demonstrate. TigerBeetle and Formance are design/test references and optional future comparative baselines, not mandatory authoritative databases beside PostgreSQL.

**Own Chronicle's temporal and analytical contract**: what a user means by “then,” what source evidence permits a version to appear, how a complete dataset becomes visible, which outputs revisions invalidate, and what evidence supports an explanation. XTDB could replace a substantial temporal storage layer if a measured need justified the change. dbt snapshots plus DVC may be sufficient for ordinary warehouse history and reproducible files; they do not automatically supply Chronicle's source-availability evidence, historical-gap handling or row/window-level revision contract. The register and Chronicle blueprint state these limits without claiming a new market invention.

**Do not add at launch:** Kafka, Kubernetes, Redis/Celery, a second analytical engine, a general DAG scheduler, a custom consensus layer, an agent framework, GPU infrastructure, a second AI assistant inside the ledger, or a generic cross-project framework. These are deferred until a concrete experiment or workload demonstrates a need. Chronicle may later compare PostgreSQL with DuckDB over immutable files; that is a measured switching decision, not a prerequisite.

## 4. Sequential versus parallel implementation

The user has not adopted a LedgerOps-first software gate. Personal learning is never such a gate. The alternatives are:

| Option | Benefit | Cost or risk | Assessment |
|---|---|---|---|
| Complete LedgerOps R1 verification before any Chronicle code | One major schema/concurrency review at a time; fast closure on one artifact | Delays discovering Chronicle's distinct time/rights/publication problems; creates an unnecessary dependency | Reasonable if the user later prefers focus; not assumed approved |
| Broad parallel implementation of every release | Maximum AI-generated implementation throughput | Large overlapping review surface; choices can harden before review; rework accumulates | Allowed by the user's AI-lead preference, but poor default for unreviewed foundational contracts |
| **Parallel narrow executable slices, with explicit decision records** | Both major unknowns become testable early; no artificial project dependency | Requires disciplined review packets and clearly marked provisional choices | **Recommended** |

Recommended launch work is independent:

- **LedgerOps:** implement the first real PostgreSQL posting/idempotency slice and its concurrent-debit and committed-reply-loss tests.
- **Chronicle:** implement the first immutable revision/snapshot slice, synthetic 100→103→withdrawal traces, source-as-of versus actual-system-snapshot API, and deterministic report over retained fixture artifacts. C2 then adds dynamic file publication and its crash/GC tests. Source-rights uncertainty does not block synthetic implementation.

This does not mean “two enormous PRs at once.” Each repository has dependency-ordered PRs in its implementation document. Present one concise consequential decision packet at a time to fit the approximately six project hours within the user's broader 12–15-hour career-work budget. AI may implement multiple future releases under explicitly provisional assumptions, isolated from verified releases; no one-release cap is introduced. If assumptions change, mark affected work for review rather than pretending approval already existed.

**Logical dependencies:** a payout recovery path depends on a ledger posting/hold contract; incremental report maintenance depends on a precise full-recompute definition; an AI answer requires stable deterministic tools. **Optional preference:** which repository receives attention first. Integration depends on both export/import contracts, but neither standalone system depends on integration. There is no promised completion date.

## 5. Reviewable releases and stopping boundaries

| LedgerOps release | Demonstrable result | Chronicle release | Demonstrable result |
|---|---|---|---|
| R1 | Transaction core, identity/replay and real interleavings | C1 | Synthetic time/revision/snapshot contract and deterministic report |
| R2 | Independent receipt/journal checks and projection rebuild | C2 | Direct-source archive adapters, rights checks and dynamic file/snapshot lifecycle |
| R3 | External unknown outcomes with authoritative recovery | C3 | Revision-aware recalculation, reproducibility bundles and controlled data/code comparison |
| R4 | Versioned EOD matching, fencing and measured batch behavior | C4 | Useful investigation UI, query/performance/cancellation/restore controls |
| R5 | Operator authorization, stale approvals, migration/restore drills | C5 | Read-only structured-tool AI and held-out evaluation |
| R6 optional | Narrow evidence export/investigation integration | C6 optional | Carefully scoped offline ML, only if a suitable corpus and evaluation justify it |

The detailed documents are authoritative for release scope and prerequisites. A release has a finite acceptance boundary; future work does not turn a finished smaller release into “nothing.” No large output is justified solely by being generated quickly. The optional ML extension must identify target vintage and label availability; a temporal data platform alone is not ML research.

## 6. Status and evidence rules

Three statuses travel separately: **implementation exists**, **verification evidence exists**, **personal ownership demonstrated**. The [shared evidence contract](02-CROSS-PROJECT-CONTRACT.md#7-shared-evidence-format-without-a-shared-framework) gives a proposed machine-readable representation. All three are currently unestablished for the two new systems.

The package contains inspected source facts and proposed contracts. It contains **no application repository, application commit, passing test run, benchmark, deployed service, real-model evaluation, customer adoption or new mastery result**. Commands and file/module paths in implementation documents describe future work. Markdown structural checks of this package, if reported, are document checks only.

A tested AI-assisted feature can be documented honestly before the user personally masters it. Conversely, a README statement must not become a CV claim of independent architecture/design without supporting ownership evidence. This package does not rewrite the CV or manage the learning tracker.

## 7. First consequential user decisions

The complete release tables contain 3–5 decisions per release. Before first implementation, present these initial choices with the supplied recommendation, allow questions and record the response or explicit delegation:

| Decision | Recommendation | Why it matters / reversal cost |
|---|---|---|
| Ledger money model and first currency | PLN, scale 2, integer minor units; customer/merchant balances are liabilities; reservations do not create posted cash movement | Changes posting examples, public API and historical interpretation; expensive after persisted data |
| Ledger concurrent update authority | Ordered PostgreSQL row locks plus checked posting routine; first compare a correct conditional-update variant under contention, with SERIALIZABLE an optional third variant | Controls transaction structure and retry behavior; moderate early, high after multiple write paths |
| Ledger operation versus transport identity | Stable operation UUID independent of scoped request key; preserve result and conflict behavior for fixture lifetime | Required for replay, unknown outcomes and restore; expensive to repair retrospectively |
| Chronicle meaning of “as of” and source gaps | Separate latest-corrected, source-as-of and exact system snapshot; unknown availability remains unknown | Defines every historical claim; expensive after data/API consumers rely on ambiguity |
| Chronicle initial storage and publication | PostgreSQL authoritative rows/metadata, immutable evidence files with explicit publish/read-pin/GC protocol | Simpler first query topology; future engine change possible via immutable contracts |
| Initial data and AI rights boundary | Synthetic complete default; narrow separately sourced archival example; FRED persistent/AI adapter disabled pending compatible permission | Avoids a false reproducibility premise and a data-rights dependency; adapters can be enabled later with recorded evidence |

These rows are not questions blocking delivery of the blueprint. They are concrete review inputs. Each project's first task is fully specified under the recommendations, so a reviewer can accept, modify or reject the design without another broad research round.

## 8. What Codex should scrutinize most critically

1. **Accounting enforcement:** a balanced multi-line journal, the materialized balance and the persisted operation result must commit together. Check permissions and deferred checks using the API role, not just an all-powerful test owner. Confirm debit/credit examples agree with the schema.
2. **Unknown external outcomes:** check the separation of transport attempts, provider observations, holds and terminal knowledge. Verify that an old backup cannot recreate an already executed business operation under a new identity. An ordinary negative lookup must remain inconclusive.
3. **Independent truth:** expected merchant ownership must originate from the frozen instruction corpus. A balanced ledger or matching own exports does not prove correct business effect.
4. **Publication authority:** fencing must be checked atomically by the result sink, and approvals must bind the exact evidence/rule/action version at execution. A token or version field without a conditional write proves nothing.
5. **Chronicle time and membership:** a snapshot must name its committed input batches; no guessed sequence watermark or backdated ingestion. Explicit date precision and tie policy must survive SQL optimization.
6. **File/database failure boundary:** a PostgreSQL transaction does not write filesystem objects. Verify write/flush/hash/publish order, pinned readers, orphan collection and restore of the full object closure.
7. **Incremental completeness:** corrections, withdrawals, missing months, shifted windows and code changes must equal the separate full-recompute oracle. Changed output counts or hashes alone are insufficient.
8. **AI policy outside prompts:** check immutable context, allowed tools, resource budgets, evidence IDs, numeric claims, abstention and malicious source text. Mocks do not measure model-answer quality, and evidence filtering does not erase model pretraining knowledge.

## 9. Remaining uncertainty and targeted experiments

| Unknown | Smallest useful next check | Consequence if the default loses |
|---|---|---|
| Row-lock versus conditional-update behavior under target skew | Same correct debit contract, bounded concurrency, identical workload and resource reporting; optionally add SERIALIZABLE with whole-transaction retries | Change one posting strategy; preserve operation and evidence contracts |
| PostgreSQL-only historical query cost at larger version counts | Scan oracle versus indexed SQL on declared series/revision/skew families; EXPLAIN and memory/time | Test a read-only analytical engine over the same immutable manifests |
| Source archive parsing stability and timestamp certainty | Independently transcribe the four historical source documents, validate parser rows/units and release evidence | Keep synthetic demo complete; narrow the accepted source adapter |
| Artifact publication/GC correctness on the actual filesystem | Pause/crash at each file/metadata boundary with concurrent readers | Restrict the supported deployment profile or change storage protocol |
| AI adds value beyond a template | Pre-registered held-out tasks, deterministic baseline, actual authorized model runs and cost accounting | Keep the deterministic product, reduce/remove agent complexity |
| Driver/runtime/image compatibility and cancellation behavior | Bootstrap exact locks, run real PostgreSQL transactions/cancellation, inspect connection cleanup | Adjust pinned builds/client settings before any performance claim |

No source reading settles these experiments. The design provides decision criteria and executable future acceptance scenarios rather than fabricated answers.

## 10. Package navigation and review readiness

- [Reference register](01-REFERENCE-REGISTER.md): version/commit, exact mechanism/test, license, transfer limits and local falsification.
- [Cross-project contract](02-CROSS-PROJECT-CONTRACT.md): independence, optional export, evidence identity and shared conventions.
- LedgerOps: [blueprint](ledgerops/BLUEPRINT.md), [contracts](ledgerops/CONTRACTS.md), [verification](ledgerops/VERIFICATION.md), [implementation and decisions](ledgerops/IMPLEMENTATION-AND-DECISIONS.md), [handoff](ledgerops/HANDOFF.md).
- Chronicle: [blueprint](chronicle/BLUEPRINT.md), [contracts](chronicle/CONTRACTS.md), [verification](chronicle/VERIFICATION.md), [implementation and decisions](chronicle/IMPLEMENTATION-AND-DECISIONS.md), [data rights and AI evaluation](chronicle/DATA-RIGHTS-AND-AI-EVAL.md), [handoff](chronicle/HANDOFF.md).

**Readiness:** suitable for engineering design review and for preparing the first implementation PRs under recorded decisions. It is not a certified design, user-approved stack, or validated system. No public repository, deployment, paid service, account, employer data, tracker or reminder was modified.

**Capabilities this does not establish:** low-latency C++ engineering, compiler/runtime implementation expertise, research-level machine learning, derivatives pricing expertise, regulated payments/compliance competence, real client delivery, commercial demand or production scale. Those claims need separate evidence; they are not prerequisites for doing these two projects well.

## 11. Final design self-review and unresolved verification

The package received a cross-document review of the accounting, identity, time, object-lifecycle and AI contracts. The review corrected several material ambiguities: payout consumption updates booked/reserved balances together; request conflicts cannot commit a provisional claim; worker acceptance checks the parent run and partition atomically; restore preserves logical provider identity; live object publication is protected against collection; parser repair creates a new interpretation generation; a higher-order revision with unknown availability can invalidate an otherwise plausible historical answer; controlled comparisons hold other context parameters fixed.

| Requested review question | Design-review result | Still unresolved by this research |
|---|---|---|
| Accounting agrees with constraints and recovery | Opening, purchase, reservation, payout and limited reversal use one chart and consistent minor units; privilege and group-check mechanisms are specified | The SQL/migrations have not been compiled or exercised; transaction, role and adversarial tests remain required |
| Unknown outcomes and restore gaps are honest | Hold state is separate from provider knowledge; no timeout-only release or new-key restore; missing history freezes the scope | Simulator finality depends on its explicit close/tombstone contract and retained history; this is not arbitrary-provider finality |
| Snapshot publication and retention survive failures | Publisher pins/tickets, guarded file promotion, atomic metadata publication, conservative GC and missing-object errors are specified | Actual filesystem, connection-loss, storage-exhaustion and backup/restore behavior require real experiments; SIGKILL is not power-loss proof |
| Source history differs from local knowledge | Source cutoffs, actual snapshot membership and precision have separate contracts; no backdated local ingestion | Four source pages establish a narrow example, not complete vintage coverage or measured intraday availability |
| Revisions invalidate all affected results | Named calendar footprints include absent inputs, downstream windows and long denominator edges; arbitrary changes use full recomputation | Independent model implementation, mutant detection and larger-data equivalence are future evidence |
| AI constraints are enforced outside prompts | Immutable contexts, narrow tools, validated claim references, server-rendered numbers and rights checks are specified | Real-model usefulness, leakage, abstention, latency and cost are unmeasured; mock success cannot substitute |
| Projects remain independent | Each has its own core demonstration and storage; optional transfer is versioned and read-only | Cross-project compatibility must be tested when export/import is implemented |
| Reuse choices have actual evidence | Deep references have inspected revisions, code/tests and licenses; uncertain version rights are recorded or excluded | Final runtime/transitive locks, notices and compatibility are not yet established; SQLite's linked fixed runtime must be checked |
| Ambition and status remain truthful | Full releases are retained, review packets are finite, AI lead is unrestricted and personal ownership is separate | No completion date, production capability, adoption, benchmark or mastery can be inferred from this document package |

**Document checks actually performed:** all 14 requested Markdown deliverables exist; relative file links and referenced internal anchors resolve; code fences are balanced; the fenced JSON examples parse; the small ledger, BLS, production-mean and four-run arithmetic examples were independently recalculated. These are checks on design documents, not application tests. The archive is separately checked for member completeness and byte integrity when packaged.

**Engineering-review readiness:** the handoffs identify an exact first patch, its contract, verification target and decision packet for each project. Codex can now review and accept, modify or reject concrete choices. The outstanding items above are explicit implementation experiments and choices, not a reason to restart broad project selection or produce another framework list.
