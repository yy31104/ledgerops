# Local make-demo verification — 2026-10-28

Base `278b41721e8d8550b70398039800702da3a7a591`. User requested a separate narrated
temporary-PostgreSQL demo, smoke coverage and interrupted-export cleanup guidance,
with no production behavior/migration changes and no self-test questions.

Result: actual **make demo succeeds**, all12 story steps execute and fixture cleanup
finishes. [Actual output](demo.log). Complete regression: **583 passed**, zero
failures/errors/skips, JUnit elapsed **46.845s**. [JUnit](pytest.xml) contains272 unit,
41 reference and270 integration cases. The two added integration cases are the
actual make-target subprocess smoke and an intentionally unavailable-Docker smoke.
The latter starts no server; it is not counted as another live PostgreSQL experiment.
The explicitly selected demo story runs inside the positive subprocess and is not
separately auto-discovered or added again to the583 count.

## Story and reuse

`make demo` explicitly runs tests/demo_story.py with existing pytest fixtures:
one fresh pinned PostgreSQL container, random local credentials, new database and
all existing migrations/roles. Existing SQL fixture helpers seed opening100; existing
purchase/reversal/maintenance/export/verifier interfaces run every operation.
Each narration follows an assertion about actual state/result, never a canned pass.

Opening100 → purchase80 returns201 with customer20/merchant80 → new purchase80 saves
422 INSUFFICIENT_AVAILABLE → merchant reversal80 returns201, customer100/merchant0
→ **fresh business ID** duplicate saves422 ALREADY_REVERSED. Same-ID replay behavior
is unchanged. Demo-only owner SQL then changes customer projection100→120, without
changing journal history. Reader export closes its connection; local files alone
feed the verifier, which reports A=false/B=true. Freeze→preview120→100→fresh atomic
repair/audit/epoch2→explicit NORMAL resume→new reader export/offline verification
returns healthy. Opening is a disclosed synthetic fixture, all amounts minor units.

TemporaryDirectory removes all before/preview/after export files. Existing fixture
teardown drops the DB/removes its own container; a second make recipe prints cleanup
completion only after pytest including teardown returns0. Ordinary exceptions fail
the command; no new Docker harness or production failpoint. This does not promise
cleanup if the process is forcibly killed or Docker itself stops during teardown.

## Smoke and final checks

`test_make_demo_runs_the_story_in_order_and_cleans_its_own_resources` invokes the
public make target, requires stages01–12 and their result markers, compares labelled
container-ID sets before/after and checks no ledgerops-demo-* directories remain.
It preserves an outer suite's existing temporary cluster, rather than requiring
global-zero while the suite is active.

`test_make_demo_fails_explicitly_when_docker_is_unavailable` clears child-only Docker
context/TLS overrides and selects a nonexistent Unix socket. make exits nonzero,
the error explicitly says Docker is required, and no story step executes.

- `UV_OFFLINE=1 make demo`: direct real run passes in2.23s; cleanup line follows pytest.
- `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/DEMO/2026-10-28/pytest.xml`: all583 pass; original581 tests/expectations unchanged.
- `UV_OFFLINE=1 make check`: Ruff clean,64 Python files formatted; forced fresh
  installed-package and five SQL-resource checks pass.
- `UV_OFFLINE=1 make build`: wheel/sdist built. [Package inspection](package-check.json)
  compares production bytes and exact final README/Makefile/demo/smoke/fixture assets
  in archives. This remains a repository demo, not a new installed production CLI.
- [Environment](environment.json) records versions, pinned server and final labelled
  container cleanup. Actual server180006 is asserted by the reused fixture.
- [Preservation](preservation.json) records unchanged src/migrations/reference/
  fixtures/old tests/prior evidence.17 reference checksums plus the original fixture
  checksum still match. No new source SHA inventory is introduced.

No failed final run or production fix. Only docs/evidence changed after the suite;
package checks used final README and Makefile. The requested .ledgerops-export-*
hidden-directory guidance was added to EXPORT-AND-VERIFY.md: reader rejection and
manual removal only after confirming no exporter is active.

## Commit and scope

[Metadata](verification.json) binds tested Git trees/tooling blobs. Locate this
separate delivery commit with
`git log -1 --format=%H -- evidence/DEMO/2026-10-28/verification.json`; the final message
reports the actual containing hash. Runtime behavior, migration bytes and accepted
R2 scope are preserved. No persistent database, self-test questions, R3/provider,
authentication adapter, push, tracker or Chronicle change. Personal mastery is not
assessed by watching or running the demo.
