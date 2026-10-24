# Local initialization evidence

Run date: 2026-10-23, Europe/Warsaw. Scope: local scaffolding only.

## State

- Implementation: independent Git repository on `main`; Python package skeleton,
  locked development tools, documentation and preserved design/review sources.
- Verification: environment installation, tooling, import and distribution checks
  below. No business functionality, application database or release is verified.
- Personal ownership: unassessed. Agent work is not evidence of Yi's mastery.
- No initial commit, Git remote, push, deployment or tracker modification.

## Verified environment

macOS ARM64; Python 3.12.13; uv 0.11.14; Git 2.54.0. Python 3.12 is the installed
interpreter used for this initialization, recorded in `.python-version` and
`requires-python`. Candidate versions in the incoming proposal are not measurements
of this repository. Other machines/platforms have not been tested.

`uv.lock` pins the development dependency resolution, including pytest 9.1.1 and
Ruff 0.16.9. Their installed metadata declares MIT licenses; this is a metadata
inspection, not a full dependency/security audit. The Hatchling build backend is
pinned to 1.32.4 in `pyproject.toml`; isolated build transitive dependencies are not
claimed to be fully locked by the project lockfile. There are no application runtime
dependencies yet. Each repository owns its `.venv` and ignored `.cache/uv` directory.

## Executed checks

| Check | Actual result |
|---|---|
| `uv lock --python /opt/homebrew/bin/python3.12 --cache-dir .cache/uv` | Success; 8 resolved packages including the local package and a Windows-only dependency. |
| `uv sync --locked --cache-dir .cache/uv` | Success; editable local package and development tools installed. |
| `UV_OFFLINE=1 make bootstrap check build` | Exit 0 using the populated local cache; lint, format, package import, sdist and wheel passed. |
| `uv pip check --python .venv/bin/python --cache-dir .cache/uv` | Exit 0; 7 installed packages compatible on this platform. |
| `UV_CACHE_DIR=.cache/uv uv run --locked --offline pytest --collect-only` | Exit 5; 0 tests collected. This confirms the test configuration loads; no test suite passed. |
| Git state and ignore rules | `main`; no remote or tracked files; `.venv`, `.cache`, `dist` and `.env` ignored. |
| Preserved-source SHA-256 checks | All 14 original documents match their manifest; all 17 copied reference files match `SHA256SUMS`. |
| Actual wheel/source archive inspection | Package/declared source files present; no `docs/`, environment files or virtual environment included. |

Initial dependency resolution/installation used PyPI. Subsequent bootstrap, checks
and build succeeded with uv offline mode through `UV_OFFLINE=1`. The offline
result relies on the already populated cache; a fresh machine needs dependency access.

## Remaining work

The first implementation contract is [R1-PR01](tasks/R1-PR01.md). Typed business
contracts, meaningful tests, PostgreSQL roles/schema/queries, failure scenarios and
release acceptance remain future work. No numerical, performance, temporal,
accounting, concurrency or model-quality claim follows from these scaffold checks.

Reference copies stay unchanged; write amendments in working ADR/task documents.
Use [HANDOFF](../HANDOFF.md) as the continuation entry point.
