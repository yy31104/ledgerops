# R1-PR01 local verification — 2026-10-23

Result: **165 passed; 0 failures, errors or skips** in 8.649 seconds.
JUnit start: `2026-10-23T18:29:41.775304+02:00` (Europe/Warsaw).
This is PR01 foundation acceptance, not complete R1 purchase/replay acceptance.

## Executed checks

| Check | Actual outcome |
|---|---|
| `UV_CACHE_DIR=.cache/uv uv run --locked python -m pytest -q --junitxml=evidence/R1-PR01/2026-10-23/pytest.xml` | 91 unit + 18 independent reference + 56 real PostgreSQL integration = 165 passed. |
| `make check` | Ruff clean, 25 Python files formatted; non-editable package imports and packaged SQL resource found. |
| `make build` | Wheel and sdist built successfully; rebuilt after README delivery update. |
| Archive inspection | Wheel includes migration SQL; sdist includes tests, reference and frozen fixture; private reference documents excluded. |
| Reference checksums | All 17 preserved documents match `docs/reference/SHA256SUMS`. |
| Frozen fixture checksum | Matches `fixtures/golden/SHA256SUMS`; value below. |
| Resource cleanup | `docker ps -a --filter label=ledgerops.scope=disposable-test` returned no containers after the suite. |

A preliminary three-test migration run also passed. Earlier collection attempts
failed before tests ran because Python ignored an editable-install `.pth` marked
hidden on this Mac. The committed configuration loads source explicitly for pytest
and tests package installation separately without editable mode. Those failed
collection attempts are not counted as verification passes.

## Environment and binding

- macOS 27.0.1, arm64; Python 3.12.13.
- uv 0.11.14; pytest 9.1.1; Ruff 0.16.9; hatchling 1.32.4 build requirement.
- psycopg / psycopg-binary 3.3.6; linked libpq `180006`.
- Docker client/server 29.4.3.
- PostgreSQL `server_version_num=180006`, asserted during cluster setup.
- Image: `postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`.
- Ephemeral localhost port, tmpfs data, random credentials; one fresh database per
  integration test. Runtime assertions connect as actual `ledger_app`, not merely
  as a privileged user claiming application behavior. Owner/admin connections are
  explicitly reserved for setup, observation and intentional guard mutation.
- Connections use UTC, 10-second statement and 5-second lock timeouts; concurrency
  tests impose their own bounded waits. No sleeps determine the racing schedule.
- Git branch `main`, no commits or remote. Source identity is a SHA-256 manifest,
  not an invented commit/release hash. Build files remain ignored local artifacts.

[Source manifest](SOURCE-SHA256SUMS) covers source, tests, reference implementation,
fixtures and build/test configuration plus the delivered README. Paths are relative
to the repository root. [Artifact manifest](ARTIFACT-SHA256SUMS) covers the
[JUnit result](pytest.xml) and built archives. Documentation-only delivery updates
followed the test run; tested Python/SQL/test bytes did not change afterward.

Frozen `purchase_80.json` SHA-256:
`3f2cdab26da61a95bf5a7648dccafffe18fd0a67af7f8f0b03892132784091a3`.

## Acceptance mapping and limits

- Money/input tests reject float/bool/noncanonical text, bad IDs, invalid key lengths
  and overflow. Literal canonical bytes and hash-collision simulation establish that
  equality uses payload bytes, not hash equality alone.
- SQL tests check finite integer totals, generated balances, scoped references,
  immutable account identity, pending→terminal transitions and terminal retention.
- Permission tests use the runtime login to prove allowed column writes and deny
  forbidden DML, history mutation, DDL, temporary tables, guard disabling and role
  escalation. The migration owner remains trusted and can disable guards.
- Journal tests reject empty, incomplete, unbalanced and declaration-mismatching
  journals at real COMMIT. Balanced historical appends fail, including two concurrent
  appenders and an attempt racing uncommitted creation. Unexpected SQL errors,
  timeouts and uniqueness conflicts cannot masquerade as the expected protection.
- An isolated mutation disables declaration triggers. The same historical-append
  assertion used for normal acceptance then fails, demonstrating defect sensitivity.
- The frozen 100/80 example folds to clearing/customer/M1/M2 = 100/20/80/0, including
  the idle merchant. Missed projection updates and projection-only writes really
  commit and then fail the independent fold. A consistent transfer to M2 commits
  and passes the fold but fails the original-request ownership assertion.

These checks do not implement production admission or establish full business
correctness. SQL still permits an ACCEPTED operation without a journal, and does
not bind a request's payload/result to its referenced operation. Ownership, request
binding, atomic posting and replay belong to PR02; concurrent debits and response
loss to PR03. Fixture SQL is test setup, not a production purchase path. No reversal,
external provider, HTTP API, deployment, tracker update or personal-mastery claim
is included.
