# R2-PR04 integrated acceptance — 2026-10-28

**581 passed**, zero failures/errors/skips; JUnit elapsed **44.625s**.
272 unit, 41 reference, 268 real PostgreSQL integration cases.
Ten new real-component cases, plus the unchanged 571-case regression.
[Actual JUnit](pytest.xml), [commands](checks.log), [acceptance matrix](../../../docs/R2-ACCEPTANCE.md).

Conclusion: R2 passes its declared bounded local ledger-core acceptance. No production
defect was found in this round; runtime, migrations, reference model, frozen fixtures
and prior tests/evidence are byte-identical to PR03. New coverage and documentation
complete the existing R2 contract, not a new provider/API/deployment milestone.

## Commit identity

Base `82fa8ffef3ae0c2cfe5fd5883c58ab9a73cc4595`. User explicitly accepted the two prior
commits and authorized PR04, asking only that disabled-customer404 be documented.
This is one separate local commit with tests, contracts and actual evidence.

Locate the delivery with
`git log -1 --format=%H -- evidence/R2-PR04/2026-10-28/verification.json` and inspect
`git diff 82fa8ff..<that-commit>`. [Metadata](verification.json) records the tested
source/tests/fixture/reference Git trees and unchanged package-tooling blobs. The
final message reports the actual containing commit hash; embedding that hash inside
its own bytes would be circular. There is no new SHA source inventory.

## New evidence

| Case | Actual observed result |
|---|---|
| Purchase40 then reversal80 | Hold first balance row, observe ordered waiters, release. Purchase saves422 insufficient; reversal201; literal clearing/customer/merchant/idle=100/100/0/0. |
| Reversal80 then purchase40 | Observe opposite order. Both201; literal100/60/40/0. Same identities/outcome rules, different permitted schedule. |
| Purchase/reversal/replay/freeze | Observe purchase10 balance wait, reversal original→balance wait, original purchase's new-key replay→reversal original wait, freeze→shared-holder wait. Release balance; purchase201, reversal201, original retained201, then freeze. Literal100/90/10/0, complete journals/request link. All business paths503 while FROZEN with unchanged financial state. Explicit resume succeeds. |
| Different originals | Purchase50 twice, then reverse two distinct originals while observing shared balance waits. Both201, two distinct success relations, final100/100/0/0. Original rows unchanged; independent fold/B pass. |
| Wrong attempt uniqueness | Add test-only UNIQUE(workspace,original) to attempts after saved refused R-A. Fund40; eligible R-B fails the **same new-success assertion** at the exact wrong constraint23505 and rolls back. Remove only that constraint; same R-B returns201, oldR-A still422, literal200/100/100/0 with reserve100. |
| Missing original serialization | Strip only the original FOR UPDATE in per-test cursors. Both server observations actually read no success, and both remain financially eligible. Release first then second: first201, second actual success-PK23505. The **normal one201/one422 oracle fails**; DB still has one effect and loser state fully rolls back. Normal retry with same loser ID saves ALREADY_REVERSED. |
| Missing intent recheck | Preserve an eligible preview, then privileged fixture corrupts source merchant. Normal application refuses with WRONG_OWNER and unchanged state. Patch only the maintenance intent flag; the **same refusal oracle fails** because repair commits. Unpatched independent B still reports WRONG_OWNER despite a now-matching projection. This demonstrates why repair needs B. |
| Actual exporter interruption, two phases | Real reader-connected child process completes data fsync, or manifest fsync, then reports an event and blocks before publication. Parent confirms child alive and kills it. Only the recognized temporary bundle remains beside an older complete export; loader rejects temp, older bytes and healthy verification are unchanged. |
| Disabled original customer | Actual new reversal404; connection IDLE and complete financial snapshot unchanged, no operation/request/attempt record for new ID. Reactivate customer, same ID succeeds. Disable again, authorized retained201 replays without new effects. |

Mixed-case expectations are literal constants declared before exercising admission;
independent reference fold and separate exported-row B also check end states. Real
pg_stat_activity/pg_blocking_pids and NOWAIT/event handshakes establish schedules.
Watchdogs/timeouts, setup errors and SQL failures are never normal business success.

Mutants deliberately run in disposable DBs/cursors or a local monkeypatch context.
Assertions first reject the wrong business result and then check the precise defect
and surviving safety bound.23505 demonstrates a broken protocol when the normal
contract calls for422; it is not mislabeled a passing business result. Production
guards/code are unchanged. Existing isolation/shared-barrier/stale-target mutations
remain in the full regression alongside these cases.

## Final checks and preservation

- `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R2-PR04/2026-10-28/pytest.xml`: 581 passed, zero failed/error/skipped cases.
- `UV_OFFLINE=1 make bootstrap`: lockfile initialization passes in the existing local
  environment. The real test fixture separately provisions a fresh isolated cluster/
  roles and new database/migrations per DB case; no existing business DB is used.
- `UV_OFFLINE=1 make check`: Ruff clean, 62 Python files formatted, forced current
  non-editable install imports reversal/maintenance and all five SQL resources.
- `UV_OFFLINE=1 make build`: wheel/sdist built. Installed CLI help and verification
  of frozen formats1/2/3 pass. [Package inspection](package-check.json) compares all
  production Python/SQL bytes, exact final README/new tests and unchanged fixture
  presence in archives. This is not full source-package release qualification.
- [Environment](environment.json) records versions and final disposable-container
  cleanup. PostgreSQL18.6 is pinned by digest; server180006 asserted by setup.
- [Preservation](preservation.json): src/reference/fixtures, all previous tests,
  historical evidence and migration bytes match base. All17 preserved reference
  checksums plus original purchase fixture checksum still pass. Git objects bind
  the source identity rather than another manual SHA source list.

No failed final full run. During mutation-test development, the server-observation
assertion initially expected client `%s` placeholders rather than server `$1/$2`;
the observation was corrected, then all three targeted mutants and the full suite
passed. This was instrumentation, not a successful business/fault assertion. Only
documentation/evidence changed after the final suite; packaging used final README.

## Completion limits and handoff

[The matrix](../../../docs/R2-ACCEPTANCE.md) maps every R2-CHECK/DRIFT/OWNER/EXPORT/
FILES/DRAIN/REPAIR/APPLY/RESERVED/REV-*/AUTH/PERM requirement to concrete current tests,
and documents the review boundary. Resume requires structure/A and preserves
B/reserve warnings. Applying repair still requires structure/B/zero reserves.
Disabled-customer eligibility returns404 without retention; financial refusals422
remain retained. No policy was changed for this boundary.

The lock conclusion covers the current cooperating single-workspace/one-original
paths and observed schedules. It is not proof against arbitrary SQL, admins,
future batches or every deadlock; bounded40P01/40001 retry and timeouts remain.
DB freeze is not proof external payments stopped. Retained canonical payloads do
not independently prove original external request capture; OPENING fixture intent
and reservation lifecycle remain disclosed limits.

R2 local acceptance is complete. Demo/self-test exercises, persistent DB, HTTP/token
adapter, external provider, R3 and R5 work are not started. Yi's accepted decisions
are recorded; personal independent mastery remains unassessed. No push, tracker,
Chronicle or original source-reference edits.
