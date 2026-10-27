# R2-PR01 verification — 2026-10-27

Result: **358 passed, zero failures/errors/skips**, 23.176 seconds: 165 unit,
41 independent reference and 152 real PostgreSQL integration cases. This adds
121 cases to the 237-test R1 regression. [Actual JUnit](pytest.xml).

## Revisions and delivered scope

Base/adoption commit: `328733b871e12c5e4b096efe2db0a25793266123`. It separately records
the user's five decisions, including retained-payload intent and fresh transactional
repair without candidate tables. R1 implementation remains at `9add0b8` plus unchanged
financial source. This delivery implements export/checking, not maintenance/reversal.

Identify this record's containing implementation commit with:

```sh
git log -1 --format=%H -- evidence/R2-PR01/2026-10-27/verification.json
git diff 328733b..<that-commit>
```

[Verification metadata](verification.json) binds tested src/tests/reference/fixture
Git trees and package-check tooling blobs. A containing commit cannot hardcode its own
hash without circularity; the final user message reports the actual delivery hash.
There is no new SHA-256 source inventory. Data/payload hashes serve data integrity.
Existing reference and R1 evidence manifests remain unchanged.

Delivered modules: `ledgerops.export` (consistent read and bounded bundle),
`ledgerops.verification` (stdlib-only independent checks), local CLI, reader migration
002, and a stdlib-only test command model/frozen reversal trace. `admission.py` and
migration 001 are byte-identical to the base. The only pre-existing test edits are
reader fixture setup and migration count 1→2; existing financial assertions remain.

## Evidence of fault detection

- Actual purchase → actual ledger_reader login → export → offline check succeeds
  without a hand manifest. Original payload bytes survive, including idle accounts.
- A cursor pauses after the balance SELECT executes; pg_stat_activity confirms the
  query/transaction state. A purchase commits before remaining export reads. The
  real REPEATABLE READ export retains the complete old state and passes. Mutating
  only isolation to READ COMMITTED mixes old balances/new entries and fails the
  **same original expected-snapshot assertion**, also reporting projection drift.
  A fresh export observes the complete new state. Sleeps/timeouts do not prove success.
- Committed projection-only corruption fails A while B passes. An owner-controlled
  corruption fixture commits M2 entries and matching projections while payload says
  M1; A passes, B reports WRONG_OWNER and blocks source eligibility. Test-only guard
  changes are restored; production guards are not weakened.
- Missing idle projection is repairable drift. Invalid canonical payload/digest,
  malformed terminal results and request bindings fail. Deeply nested payload JSON
  returns an issue instead of escaping as RecursionError. Nonzero reserves prohibit
  rebuild eligibility; they are not reconstructed from entries.
- Actual reader credentials cannot mutate rows, create/alter objects, create temp
  tables or acquire app/migrator authority, even outside a READ ONLY transaction.
- Row/byte limits, large-payload preflight, scope filtering and future-schema rejection
  run against PostgreSQL. Offline loader tests exercise corruption/truncation, duplicate
  keys, unexpected members, symlink/FIFO inputs and strict JSON types.
- File fsync/rename faults before publication leave no completed package. A parent
  fsync failure after rename may leave a valid final package and is explicitly tested
  as an ambiguous publication result, not falsely claimed absent.
- The independent, hand-calculated trace retains a failed reversal, adds funding,
  succeeds under a new business ID, replays old outcomes and permits only one success.
  An extra hand-input check detects when payload+entries+balances consistently use
  the wrong merchant. These are semantic model tests, not production reversal tests.

## Commands and environment

| Command/check | Actual result |
|---|---|
| `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R2-PR01/2026-10-27/pytest.xml` | 358 passed; zero errors/failures/skips |
| `UV_OFFLINE=1 make check` | Ruff clean, 46 Python files formatted; fresh installed CLI/export/verifier and both SQL resources import |
| `UV_OFFLINE=1 make build` | Wheel and source distribution built |
| Installed `python -m ledgerops.cli --help` | Passed after fresh reinstall |
| Wheel/sdist inspection | All 13 production Python/SQL files byte-match current source; exact root README, Makefile, new model/fixture/test included; private docs excluded |
| Historical preservation | All 17 reference checksums + original fixture checksum pass; protected paths match base |
| `docker ps -a --filter label=ledgerops.scope=disposable-test` | No remaining test container |

See [command output](checks.log), [package inspection](package-check.json) and
[exact component versions](environment.json). PostgreSQL 18.6 is pinned by digest;
the fixture asserts server_version_num 180006. Actual app and reader logins run in
new isolated databases. Docker absence fails, rather than silently skipping cases.

During final packaging checks, the previous basic installed import used uv's cached
older local package and did not include the new CLI. The installed CLI smoke failed.
`make check` now forces `--reinstall-package ledgerops` and imports all new modules
plus migration 002; check/build/CLI smoke then passed. The first ad hoc sdist README
assertion accidentally selected reference/README.md by suffix; exact root selection
fixed the inspection. Both observations are retained in checks.log. Application tests
use current src explicitly and did not depend on the stale installed package.
Only Makefile/docs/evidence changed after the full suite; package checks were repeated.

## Limits and next slice

B establishes agreement with retained canonical purchases, not independent proof of
external request capture. Legacy OPENING payloads lack canonical funding intent;
reports disclose that limitation. Unknown future financial schemas fail closed.
Bundle size is bounded to 16 MiB total and 10,000 rows per table; this is local,
single-host tooling. Integrity hashes are not authenticity signatures.

`rebuild_eligible` is a source-check result, not maintenance permission or assurance
of current DB freshness. PR02 must freeze/drain, preview, recompute/recheck under
the exclusive transaction, audit old/new/epoch, re-export, then explicitly resume.
PR03 adds production full reversal; PR04 integrates its fault schedules. Database
freeze never establishes that external payments stopped.

Yi's accepted decisions are recorded; independent mastery is unassessed. No remote
push, tracker update, Chronicle edit, persistent demo DB or HTTP service was added.
