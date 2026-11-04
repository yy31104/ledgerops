# Chronicle — engineering handoff

Prepared 2026-10-23. **Actual state: design and source research only.** No repository, implementation, passing test run, benchmark, deployment or personal-mastery result exists.

## Goal and accepted boundaries

Build the selected Chronicle flagship: historical source revisions, immutable snapshots, reproducible reports, incremental recalculation and an evaluated read-only AI interface. It works independently of LedgerOps. No trading, employer data, paid calls or publication is authorized by this research. AI may implement multiple releases ahead of learning; present 3–5 consequential decisions before each release. Track `implementation`, `verification` and `personal_ownership` separately.

## Recommended design — not adopted approval

Python 3.13, FastAPI, explicit Psycopg 3 and PostgreSQL 18. PostgreSQL owns revision rows and exact snapshot membership; immutable files retain allowed evidence and bundles. Exact dependency locks must be tested during implementation. No second analytical engine is required initially.

Three modes: latest-corrected resolves current once; source-as-of applies evidenced availability within a pinned archive; system-snapshot selects an actual local snapshot ID. Date-only evidence cannot establish intraday availability or past local ingestion. A newer revision with unknown availability can make a strict historical answer uncertain even when an older evidenced value exists.

Start with synthetic fixtures. First real corpus: four direct BLS/Board publications, supporting employment monthly change and production three-month mean. **FRED/ALFRED persistence and AI remain disabled under the proposed rights policy.** Direct-source rights and precision have separate records; no key is needed for C1.

## Unique next task

After implementation is authorized, implement **C1-PR1** from [IMPLEMENTATION-AND-DECISIONS.md](IMPLEMENTATION-AND-DECISIONS.md#9-first-exact-implementation-task): typed time/revision/unit contracts, frozen G1–G4 expected outputs and independent scan/rational reference models. Present C1-D1–D4 for selection/delegation; reversible scaffolding may use labelled assumptions. No LedgerOps release or learning check blocks this task.

Proposed future commands: `make bootstrap`, `make test-contracts`, and `python -m chronicle.cli demo-synthetic --fixture tests/fixtures/golden/revision_timeline.json`. They do not exist or run yet. C1-PR2 adds real PostgreSQL verification.

## Inspect most critically

- [CONTRACTS.md](CONTRACTS.md): publication identity differs from row locator; source order needs evidence; parser repairs create a new interpretation generation.
- File promotion and GC share object locks, including absent metadata rows. Snapshot publication checks exact membership/object closure and expected parent. TTL expiry alone does not justify deleting live work.
- Calendar dependencies include missing inputs and downstream windows. Incremental output must equal independent full recomputation.
- Controlled data × code comparison uses four child contexts with fixed mode/cutoff/parameters and explicit interaction.
- [DATA-RIGHTS-AND-AI-EVAL.md](DATA-RIGHTS-AND-AI-EVAL.md): rights, contexts and claim validation are enforced outside prompts; quality claims require actual model trials.

## Evidence and remaining decisions

Feast/XTDB/historical dbt source, tests and licenses were inspected, not executed. [VERIFICATION.md](VERIFICATION.md) defines unexecuted acceptance, fault and benchmark work. First choices: storage/time/numeric contract, then direct-source rights, retention and later model budget. Filesystem behavior, dependency compatibility and performance remain unverified; four publications do not establish complete vintage coverage.

Do not alter trackers, reminders or old projects; do not register accounts, contact publishers or spend/publish. Optional ledger import follows [02-CROSS-PROJECT-CONTRACT.md](../02-CROSS-PROJECT-CONTRACT.md). Ready for engineering review; no implementation acceptance is claimed.
