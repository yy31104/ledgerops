# Local demo slice

Date: 2026-10-28. Base: `278b41721e8d8550b70398039800702da3a7a591`.
The user accepted R2 closure and requested one separate local make-demo slice,
without production behavior/migration changes or personal self-test questions.

Status: complete locally. Actual [demo output](../../evidence/DEMO/2026-10-28/demo.log)
and [verification](../../evidence/DEMO/2026-10-28/README.md) record12 checked stages,
automatic cleanup and583 passing regression cases, including two make-target smokes.

## Contract

`make demo` in the repository root initializes a temporary pinned PostgreSQL using
the existing pytest cluster/database/role fixtures, executes real interfaces and
checks each result before printing a short Chinese sentence. No persistent database,
external DSN, provider, authentication adapter or production feature is added.

Story: fixture opening100 → purchase80 success → new purchase80 refused422
INSUFFICIENT_AVAILABLE → full reversal80 success → new-business-ID reversal refused422
ALREADY_REVERSED → demo-only customer projection100→120 corruption → consistent
export and offline A=false/B=true → freeze → preview120→100 → live recompute/apply
with epoch1→2 and immutable audit → explicit NORMAL resume → new export/offline
healthy check. Financial amounts are synthetic minor units. The duplicate uses a
fresh business ID, because retrying the same accepted reversal replays201.

## Minimal reuse and cleanup

`tests/demo_story.py` is explicitly selected by make, outside normal test_*.py
discovery. It reuses existing fixture SQL and purchase/reversal inputs, admission,
maintenance, export and stdlib-only verifier. It uses TemporaryDirectory for all
export/preview files and closes reader connections before offline verification.
Existing fixture teardown drops the fresh DB and removes its own container. Make
prints cleanup completion only after pytest, including teardown, exits successfully.
Errors propagate as nonzero exit; missing Docker is explicit, not silently skipped.

The normal regression includes a subprocess smoke of the public make target. Check
all12 stages, final health, own-container cleanup while preserving any outer test
cluster, and absence of temporary demo exports. A second smoke directs Docker to a
nonexistent socket after clearing inherited context/TLS overrides; require explicit
failure before the first story step. No alternate Docker harness or new failpoint.

Add the requested export-document sentence: interrupted exports can leave hidden
`.ledgerops-export-*` temp directories, the loader rejects them, and the user may
manually remove them after confirming no exporter is active.

## Delivery

Record actual make-demo output and smoke/regression/tooling results with the Git
base/delivery and tested Git objects. One local commit; no push, tracker, Chronicle,
new migration, production behavior change or self-test exercises. R2's previously
accepted scope remains complete; the demo is a separate explanatory wrapper.
