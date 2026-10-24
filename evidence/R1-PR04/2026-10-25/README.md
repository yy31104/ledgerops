# R1-PR04 verification — 2026-10-25

Result: **237 passed, zero failures/errors/skips**, 27.768 seconds. The existing
suite is unchanged: 91 unit, 18 independent reference and 128 real PostgreSQL cases.
JUnit start: `2026-10-25T00:02:16.129885+02:00` (Europe/Warsaw).

## Revisions and scope

The received R1-PR01–PR03 implementation is preserved in local baseline commit
`feb458864c293d77c838913e720e878f09b96846`. Its 90 files include original reviews,
design references and historical evidence. PR01/02/03 were not reconstructed as
fictional separate commits. Preserved failed-run XML retains its original whitespace.

This record is delivered with the independent PR04 extraction commit. On its checkout,
`git log -1 --format=%H` identifies that delivery; `git diff feb4588..HEAD` shows its
complete change. Exact tested code identity is also recorded in
[verification metadata](verification.json):

- `admission.py` Git blob: `4679453bdbfb609601a5e95de643442508cb9dfd`.
- Baseline `tests/` Git tree: `beb6960fe2dffc8812ce9b2cc5ebbd25fe068d97`.
- Production changes are confined to `src/ledgerops/db/admission.py`.
- Tests, fixtures, reference implementation, SQL schema and migrations match baseline.

Chronicle already had baseline `be5df9a` from parallel work when inspected. Its
in-flight test changes were not staged or modified by this LedgerOps session. No
Chronicle correctness claim or test rerun is made here. Secrets, local environments,
caches, databases and build outputs stay excluded by the existing ignore rules.

## Extraction

`_submit_purchase_once` is 63 source lines instead of 219, measured including its
signature, docstring and blank lines. Four extracted steps have one purpose each:

| Function | Source lines | Purpose |
|---|---|---|
| `_open_admission` | 16 | Transaction settings, shared workspace barrier, mode check and customer authorization. |
| `_replay_by_request_key` | 24 | Authorize and return an existing transport request result. |
| `_replay_by_operation` | 15 | Authorize the retained business result and finalize a new request-key association. |
| `_post_new_purchase` | 110 | Lock balances and atomically store a purchase effect or its rejection. |

The original claim order remains in the orchestrator. Helpers do not open/commit
transactions, catch new errors or move UUID generation out of its existing branches.
Existing canonical bytes are passed to replay helpers. The result still returns only
after leaving the transaction context and running deferred COMMIT checks.

The [extraction comparison](extraction-check.json) inlines helper statements into
the orchestrator, removes helper-local immutable bindings/returns, and normalizes
only SQL whitespace. The resulting AST equals the baseline transaction AST. All
other pre-existing function ASTs are identical. A second read-only agent independently
performed the same comparison and manually reviewed authorization, SQL/lock ordering,
insufficient-funds-before-limit priority, exact integer arithmetic, terminal writes,
UUID branch sites and retry/COMMIT boundaries; it reported no findings.

This structural comparison supports the extraction claim; the unchanged real-component
suite remains the behavior evidence. It is not a proof of every possible fault schedule.

## Actual checks

| Command/check | Outcome |
|---|---|
| `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R1-PR04/2026-10-25/pytest.xml` | 237 passed; zero failures/errors/skips. [JUnit](pytest.xml). |
| `make check` | Ruff clean, 36 Python files already formatted; package import and SQL resource check pass. |
| `make build` | Wheel and source distribution built. [Actual log](checks.log). |
| Wheel/sdist inspection | Wheel admission bytes match tested source; migration SQL present; sdist contains tests/reference/fixture and excludes private source documents. |
| `git diff HEAD -- tests fixtures reference src/ledgerops/db/sql src/ledgerops/db/migrations.py` before delivery | Empty; no existing test or database safeguard changed. |
| Preserved sources | All 17 reference hashes and frozen purchase fixture hash unchanged. |
| `docker ps -a --filter label=ledgerops.scope=disposable-test` after suite | No test containers remain. |

Same pinned environment as PR03: Python 3.12.13, psycopg 3.3.6 / libpq 180006,
PostgreSQL server_version_num 180006 asserted by setup, pytest 9.1.1, Ruff 0.16.9,
uv 0.11.14 and hatchling 1.32.4. Image:
`postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`.
Actual runtime login credentials exercise admission in fresh databases inside the
suite's disposable localhost-only container. Missing Docker fails rather than skips.
[Artifact checksums](ARTIFACT-SHA256SUMS) bind JUnit and the local build archives.

## Boundary

No new behavior, reversal framework, demo command, HTTP/token adapter or R2 coding
is part of this commit. Optional demo work remains a separate slice. Yi's personal
understanding remains unassessed; the three questions in the Opus review are review
prompts, not a development gate. No remote push, deployment or tracker update occurred.
