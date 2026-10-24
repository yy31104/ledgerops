# Review — LedgerOps and Chronicle Pro blueprints

Date: 2026-10-23. Classification: private project review / action item, not learner knowledge.
Status: **recommend adopting the architecture with targeted amendments; not yet a user-adopted implementation plan**.
Input: [preserved source package]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/00-EXECUTIVE-REVIEW.md).
Scope: Yi asked “How is it?” after returning the package. No implementation, paid resources, publication, tracker, reminder, thesis, application, DSA or skill changes are authorized by this review.

## Verdict

Keep both projects and this package as the main design reference. It contains concrete contracts, failure traces, source pins, test oracles and bounded implementation slices, rather than merely a technology list. It preserves the intended distinction:

- LedgerOps: transactional correctness, concurrency, uncertain external outcomes, recovery and reconciliation.
- Chronicle: temporal semantics, reproducible analysis, incremental computation and an evaluated, read-only AI interface.

The audited issues do not justify another project-selection round or a rewrite. Amend two literal contract issues and make several enforcement details explicit before their respective releases. This is approval of the **direction as a reviewer recommendation**, not a claim that the software is correct, implemented, production-grade or guaranteed to improve hiring outcomes.

## Receipt and evidence boundary

- Supplied ZIP: `$USER_HOME/Downloads/LedgerOps-Chronicle-Blueprints-2026-10-23.zip`.
- ZIP SHA-256: `11c912b30849d508f336c68d4a07c62b17ecec99b5f802dacbe9aaceacb20845`.
- Archive contained 14 Markdown documents plus `MANIFEST.sha256`; regular-file inventory checked before extraction. No upstream/application code was executed.
- Preserved original extracted bytes under the source directory above. All 14 supplied manifest hashes passed locally, including a final recheck. This is byte-integrity evidence, not proof of authorship or semantic correctness.
- Main and parallel reviewers inspected the executive/cross-project contracts, both project contract/release/verification sets and source/AI-rights register. Decision-critical external sources were sampled; this was not an exhaustive link, security or transitive-license audit.
- An independent Python stdlib Decimal/Fraction calculation reproduced the numerical counterexample below. That is a review probe, **not a Chronicle test run**.
- No PostgreSQL instance, application migration, fault suite, benchmark, upstream repository test or actual model trial was run. Pro's reported document checks remain Pro-reported unless specifically repeated here.
- Implementation, verification and personal mastery remain separate. Verified AI-built functionality can eventually be described as such; it does not establish independent authorship or mastery.

## Findings and amendments

### F1 — Rejected reversal consumes the successful-reversal uniqueness slot [P2; R1]

Source: [operation schema]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/ledgerops/CONTRACTS.md:49), [retained rejection semantics]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/ledgerops/CONTRACTS.md:152), [admission algorithm]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/ledgerops/CONTRACTS.md:193).

The schema says the original-operation reference is unique for a full reversal, while rejected operations must remain recorded. These rules conflict if uniqueness covers rejected attempts.

Counterexample: a purchase gives merchant M 80; a hold reserves 62, leaving 18 available. Reversal R1 for 80 is rejected and retained with original purchase P. The hold is then definitively released. New reversal R2 references P and has sufficient funds, but the bare unique reference already belongs to rejected R1. The full hold/release integration trace depends on R3; make the R1 schema correct now and add the end-to-end trace when that capability exists.

Amendment: enforce at most one **successful/accepted full reversal** of an original purchase, not at most one attempt. Use an appropriate partial unique index or an independent successful-reversal relation, retain rejected attempts, and serialize against the original operation. Define lock order when both original and reversal operations are involved. Target `ON CONFLICT` at the intended identity constraint so an unrelated constraint is not silently treated as a replay.

Acceptance: rejected reversal → funds restored → new operation ID succeeds; old operation still replays its rejection; two competing successful reversals cannot both post.

PostgreSQL distinguishes all-row uniqueness from subset uniqueness through a partial unique index: [official constraints](https://www.postgresql.org/docs/18/ddl-constraints.html#DDL-CONSTRAINTS-UNIQUE-CONSTRAINTS).

### F2 — Decimal precision 50 does not prove single correctly rounded output [P2; before optional C3 YoY]

Source: [numeric policy]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/CONTRACTS.md:31), [declared output domain]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/DATA-RIGHTS-AND-AI-EVAL.md:271).

The fixed precision can round an intermediate ratio onto a half-even boundary before final 12-place quantization. Independently reproduced:

```text
x = 20000000000000100000000000.000000000003
y = 200.000000000001
formula = 100 * (x / y - 1)

Decimal precision 50, then quantize to 12:
9999999999999999999999900.000000000002

Exact Fraction, then remainder-based half-even rounding to 12:
9999999999999999999999900.000000000001
```

Both inputs, intermediate magnitudes and outputs fit the stated NUMERIC(38,12) magnitude boundary. No explicit narrower series domain in the proposed contracts excludes the example. This is an extreme-domain defect, **not a claim that the four real-publication examples are wrong**.

Amendment: calculate the supported fixed formulas with integer/rational semantics and exact remainder-based canonical rounding, or define/enforce narrower bounds and prove sufficient precision. Merely changing 50 to an arbitrary larger number is not a proof. Keep the independent oracle structurally separate even if both use exact arithmetic.

Acceptance: add this case, exact ties, neighboring values, negative signs, zero denominators and output overflow to the numerical boundary corpus. Set the numeric contract before generalizing it; the first employment-delta/mean report need not wait for optional YoY implementation.

Mechanism: Python context precision rounds arithmetic; `quantize` then rounds to an exponent. [Python 3.13 Decimal documentation](https://docs.python.org/3.13/library/decimal.html).

### F3 — Make unknown-availability shadowing explicit in the independent oracle [clarification; C1]

Source: [oracle paragraph]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/CONTRACTS.md:300), versus [normative uncertainty rule]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/CONTRACTS.md:25), [query companion rule]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/CONTRACTS.md:335), and [G4]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/VERIFICATION.md:57).

The oracle paragraph says to filter known-time eligible rows and choose the greatest order. Taken literally, known order 10/value 100 plus unknown-time order 20/value 103 yields 100. The actual contract and G4 correctly require `unknown_availability/null`.

Amendment: the independently written oracle must retain membership-restricted unknown orders and apply the anti-shadow rule before yielding an authoritative number. G4 already provides the correct expected outcome; align the algorithm text with it. This is a localized specification inconsistency, not an observed implemented leak.

### F4 — Immutable evidence hashes do not establish current approval validity [P2 mechanism gap; R5]

Source: [approval contract]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/ledgerops/CONTRACTS.md:341) and [stale-approval trace]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/ledgerops/CONTRACTS.md:373).

An action approved against run R1/hash H1/account v4 can still pass the listed immutable-hash and account-version checks after corrected run R2/H2 appears, if the account remains v4. The trace expects rejection, but the contract does not specify how that new evidence invalidates the action.

Amendment: define the relevant evidence scope/head or epoch (case, statement scope or business day), bind the proposal to it, and serialize head publication/invalidation with action execution. Do not invalidate unrelated cases globally. A hash proves identity, not continuing suitability.

Acceptance: approve at H1/v4 → publish relevant H2 without changing financial state → execution rejects; test the race between publication and execution. This is due before R5, not a reason to stop R1.

### F5 — Separate safe rendered output from correct model assertions [P2 specification gap; C5]

Source: [answer schema and rendering]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/DATA-RIGHTS-AND-AI-EVAL.md:292), [AI-02/AI-09]($USER_HOME/Documents/AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints/chronicle/DATA-RIGHTS-AND-AI-EVAL.md:379).

The proposed claim has result references and free-form `proposed_text`, but no typed proposed numeric value/unit. AI-02 expects deterministic rejection of a draft saying 229 when the result says 353. Rendering the correct 353 from a reference makes the displayed answer safe, but cannot by itself establish that arbitrary draft prose asserted 229 or that the model reasoned correctly.

Choose and document one contract:

1. Ignore/drop factual draft prose, render references canonically, and evaluate reference/claim selection separately; update AI-02 to test that guarantee.
2. Require typed proposed assertion/value/unit fields and compare exactly before rendering. Do not infer the typed value from unrestricted prose.

The remaining free-text channels also need a boundary: clarification question/options, limitation prose and connective wording can contain unsupported premises (e.g. “Since monetary policy caused this revision, which cutoff?”). For C5, use approved enum/template choices for these fields too, or label/test optional prose under a weaker, unverified guarantee. A citation and a numeric template do not validate every other sentence.

Acceptance: wrong typed value, wrong selected period/unit, unsupported premise inside a clarification/limitation, and removed-validator mutants each fail for the intended reason. Report model errors separately from host-corrected output.

## Other details to settle, not reasons to reject the design

- **Freeze means draining in-flight writers.** Before R2 rebuild activation/R5 restore, define a workspace barrier shared by admission and observation writes. Test: writer checks NORMAL and pauses → freeze/export begins → writer resumes. A mode flag alone is not a write-drain protocol. The document already requires frozen writes; this makes the precondition executable.
- **Holdout wording:** B2 architecture selection should use development/validation evidence, then a fresh final suite. The phrase “same held-out tasks” is ambiguous, but the document already requires replacing exposed tasks and reserving fresh final families. Clarify rather than inventing a new evaluation process.
- **Optional cross-project adapter:** Chronicle C1 is monthly-series shaped; LedgerOps export is daily/transactional. Define a later explicit adapter/aggregation and time policy before claiming interoperability. No shared database or synchronous ledger dependency should be introduced.
- **Dependency lock remains work:** source/version/license inspection does not replace exact patch/digest/transitive locking, local compatibility tests or the proposed linked-SQLite capability check.
- **Human review:** parallel build is technically reasonable because the systems have independent contracts. Recommend one human decision/review packet at a time, not a one-release AI learning cap. Exact build order is still unadopted.

## Adoption matrix — reviewer recommendations only

| Decision | Recommendation | Reason / qualification |
|---|---|---|
| LedgerOps + Chronicle, no third worker flagship | Adopt | Distinct transaction versus temporal/analytical evidence |
| Python/FastAPI/explicit SQL/PostgreSQL first | Adopt | Visible transaction/query boundaries; exact dependency locks still needed |
| Protected PL/pgSQL admission + Python outer transaction | Adopt with explicit user choice | Strong authority boundary, but real SQL/migration/review complexity; not incidental boilerplate |
| Independent provider process/store; stable identity; close-if-absent | Adopt | Models non-rewindable external state; strong synthetic protocol, not a standard real-bank feature |
| Independent oracles, component faults, mutation checks | Adopt with F1–F3 corrections | Tests can challenge behavior rather than merely repeat implementation |
| Three time meanings, explicit snapshot membership, conservative pins | Adopt | Prevents fabricated historical knowledge and partial publication |
| Direct-source numerical archives plus synthetic path | Adopt with per-artifact rights checks | FRED acquisition/use restrictions matter; no blanket permission for all linked material |
| Deterministic B0, bounded tool-based B1, optional B2 | Adopt with F5 | AI must show value without owning arithmetic or state-changing authority |
| Evidence-bound approvals / maintenance freeze | Amend before dependent releases | F4 and drain protocol need executable mechanisms |
| Parallel narrow build slices | Recommend, not adopted | No technical cross-project dependency; human packets remain serialized |
| Automatic whole-package execution or new tracker | Reject | Review is not implementation authorization |
| “Bank-grade”, universal correctness, employer approval or mastered skills | Reject | No such evidence exists |

## Primary-source spot checks

No material factual correction was found in these decision-critical samples. This is not exhaustive legal clearance or verification of every reference.

- FRED current clauses restrict relevant API archive/storage and AI uses; the dated June 18, 2024 notice explicitly records those changes. Keeping that adapter disabled is a conservative project choice, not a claim that all economic data is prohibited. [Current terms](https://fred.stlouisfed.org/legal/), [dated notice](https://fred.stlouisfed.org/news/2024/06/weve-updated-our-terms-of-use-action-requested).
- Direct BLS numerical publications and Board information have their own public-domain notices with imagery, third-party and insignia exceptions. Scope permissions to selected tables and downstream use. [BLS](https://www.bls.gov/bls/linksite.htm), [Board](https://www.federalreserve.gov/disclaimer.htm).
- BLS archived Dec/Jan levels 157347/157700 and 157304/157533 match; 353 → 229 thousand is independently stated in the March revision narrative. Board Nov/Dec/Jan values 102.7/102.7/102.6 → 103.0/102.7/102.2 also match. This validates chosen source cells, not an implemented parser. [BLS February](https://www.bls.gov/news.release/archives/empsit_02022024.htm), [BLS March](https://www.bls.gov/news.release/archives/empsit_03082024.htm), [Board February](https://www.federalreserve.gov/releases/g17/20240215/), [Board March](https://www.federalreserve.gov/releases/g17/20240315/).
- Formance's cancellation/unlock regression and stronger test are real and fixed in the inspected release. Killing a backend could make the earlier regression test pass for the wrong reason. [PR #2133](https://github.com/formancehq/ledger/pull/2133), [pinned implementation](https://github.com/formancehq/ledger/blob/64ee64d812cfbf2e20b7d2aa5a97057a67792272/internal/storage/ledger/store.go#L165).
- Feast's pinned PostgreSQL code has the optional created-time predicate; the source register correctly distinguishes template inspection from File/Ibis behavior, not pretending to reproduce all stores. [Pinned code](https://github.com/feast-dev/feast/blob/e304a84f979b0f3b828b7c778618d7b7eaa6a2d2/sdk/python/feast/infra/offline_stores/contrib/postgres_offline_store/postgres.py#L1012), [PR #6617](https://github.com/feast-dev/feast/pull/6617).
- Sampled pinned Formance v2 MIT, Feast Apache-2.0 and Inspect MIT license claims match. Missing LICENSE in the inspected Formance v3 tree means rights were not established, not proof of wrongdoing or universal licensing status. Copying/build dependencies still require exact-scope review.
- Inspect's numeric-scoring and grader-reuse fixes support testing evaluators themselves; they do not make its generic number matcher an exact-decimal oracle. [Numeric fix](https://github.com/UKGovernmentBEIS/inspect_ai/commit/660c482516ac3fafbed8cfb856ecb3a6e8add7f4), [grader reuse](https://github.com/UKGovernmentBEIS/inspect_ai/pull/5570).

## End-to-end traces checked at design level

| Project / trace | Assessment and boundary |
|---|---|
| Ledger: two debits of 80 from 100 | Ordered locks allow one success and one persisted rejection; either winner allowed |
| Ledger: commit succeeds, reply lost | Atomic result/journal/projection plus same business identity supports replay without a second effect |
| Ledger: provider succeeds, local crash, lagging NOT_FOUND | Hold remains; authoritative success consumes it and posts once; no timer release |
| Ledger: late PUT versus close-if-absent | Serialized provider execution/tombstone makes closure meaningful; only under this synthetic protocol |
| Ledger: stale worker / old backup | Sink-side generation check and independent command/provider evidence have coherent failure outcomes; missing evidence freezes rather than invents recovery |
| Chronicle: higher-order unknown revision / withdrawal | Normative rule returns uncertainty, then withdrawal when eligible; oracle paragraph needs F3 |
| Chronicle: publisher pause / GC / lost reply | Ticket/pins, shared object locks and pointer CAS prevent partial authoritative snapshots under stated assumptions |
| Chronicle: revision / missing-backfill / withdrawal | Calendar dependency windows and absent dependencies invalidate affected outputs; full recomputation is the independent check |
| Chronicle: data × code 10/12/20/24 | 2 + 10 + 2 = 14; changed cutoff is a different comparison, unavailable code stays incomplete |

These are specification traces, not passing application tests. Physical power-loss guarantees, model usefulness, external usability and customer demand remain unproved.

## Exact next work after user adoption

1. Produce a small reviewed amendment set covering F1 and F3 immediately, numeric policy before its formulas are generalized, and record F4/F5 plus the freeze barrier as gates on their dependent releases. Preserve incoming originals.
2. Present the existing 3–5 decisions per initial release with plain-language options and recommendations; do not record defaults as Yi's decisions.
3. In explicitly authorized code locations, begin LedgerOps R1-PR01 (real PG/roles/complete-journal rejection) and Chronicle C1-PR1 (time fixtures/independent scan and arithmetic oracle). They can proceed independently.
4. Demonstrate the first complete stories before broad expansion: Ledger purchase concurrency + replay; Chronicle different historical answers + immutable snapshot + explicit uncertainty.
5. Release with actual evidence commands/results and distinct implementation/verification/personal-ownership fields. Let AI lead learning by multiple releases as requested.

Do not send the entire project back for another open-ended research round. A focused correction pass and executable verification are now more valuable.


## Additional opinion — Claude Code, 2026-10-23 (recommendation, not adopted)

Agrees with adopting the package and with F1–F5. F2 was independently re-run (Python stdlib, `-I`): Decimal precision 50 gives `…900.000000000002`, exact Fraction with half-even gives `…900.000000000001`.

Two R1 defaults recommended differently, both for Yi's decision:

- **Posting authority → choose the package's alternative for R1.** The default puts the entire admission algorithm (request claim, operation admission, posting/reservation, persisted response; later worker claims and observation transitions) in `SECURITY DEFINER` PL/pgSQL, with Python as a thin transaction shell. For a Python-backend portfolio this moves the most interview-relevant code into PL/pgSQL, adds a second language and a privilege model at the foundation, and is harder to unit-test and debug. Recommended R1 shape: Python owns admission in one explicit transaction with explicit SQL; the database still enforces the money invariants — deferred complete-journal constraint trigger, append-only journal (UPDATE/DELETE revoked plus mutation trigger), per-row projection CHECKs (`booked >= reserved`, no negative available balance), and uniqueness for operation/request identity. Bypass by other application code is a small threat in a single-app lab and is what R2 drift/rebuild checks detect. Moving entry points into the database becomes an R5 comparison experiment (can the runtime role bypass admission under each design?).
- **First reversal scope → omit from R1, add in R2.** R1's story (concurrent debits, replay, committed reply loss) is complete without it; deferring removes a second write path and moves F1 out of R1.

Withdraws the earlier sequential-start recommendation: parallel AI implementation with one serialized human decision packet at a time addresses the attention concern. Yi does not need to read the package; the next human input is the R1 decision packet plus the code repository locations (suggested `~/Desktop/projects/ledgerops` and `~/Desktop/projects/chronicle`, local only).
