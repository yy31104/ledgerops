# Concurrency learning slice — 2026-10-28

Base: ac517e3. Locate the containing local delivery commit with
`git log -1 --format=%H -- evidence/LAB-CONCURRENCY/2026-10-28/verification.json`.
Developed in an isolated worktree so the live ac517e3 page did not load partial edits.

- **609 passed in82.41s**, zero failures/errors/skips:272unit,41reference,296integration.
  See `pytest.xml`. Includes the original595 cases plus4trace/source cases and10
  HTTP concurrency/cleanup cases. The20 focused trace/concurrency tests and26
  complete lab tests were also run during integration.
- Actual worker A waits behind a real balance-row blocker; then B or freeze is
  started. Advancing requires pg_stat_activity Lock plus pg_blocking_pids and the
  expected SQL. Tests query the same owned PIDs with another independent observer
  and compare displayed state/query/blockers before releasing anything.
- Purchase race:201/422, customer20/M1=80/clearing100/M2=0;3operations,2requests,
  2journals/4entries including opening, final independent structure/A/B healthy.
- Drain: observed balance wait and freeze→writer advisory wait; release yields
  purchase201 and FROZEN, later new purchase503 with no new operation/request/entries.
  Final balances20/80 and independent structure/A/B healthy.
- Cancellation at either waiting phase, browser-refresh cleanup POST, global reset,
  active-scene replacement, server close and actual watchdog callback close worker/
  observer PIDs. The unfinished freeze case remains NORMAL with OPENING only and
  no freeze record. Dependants B/freeze are cancelled and joined before A, then the
  blocker is rolled back. This prevents cleanup from accidentally releasing freeze.
- Whole-scene watchdog300s; extra worker SQL configures lock/statement backstops10min
  after unchanged service5s/10s SQL. Extra commands are genuinely traced with lab
  source locations. Timeout is failure protection, never evidence that ordering occurred.
- Transaction source assertions locate real with-statement lines using AST for
  admission/maintenance BEGIN/COMMIT/ROLLBACK, including synthetic in-repo dependency
  frames and actual current psycopg execution. Delegating teaching cursor is skipped;
  actual extra SQL points to its helper, financial locks to _lock_balances.
- `make check`, fresh noneditable install/resource check, `make build`,
  `node --check lab/app.js`, static controls/assets and `git diff --check` pass.
  Wheel/sdist contain no lab implementation/assets or lab-only tests.
  src/migrations, fixtures/reference, old evidence and uv.lock match ac517e3.

The complete new teaching copy is in `docs/tasks/LAB-CONCURRENCY.md`. Original
twelve-step wording now distinguishes its serial actions from the new real concurrent
panel, and lesson6 refers to LabSession._drift. No quizzes, production API/auth,
runtime dependency, push, external-payment integration or personal mastery claim.

Browser automation was disabled in the prior slice; no visual claim is inferred from
HTTP/JS checks. The new page is opened via the app panel when the local service is
restarted. A final live teaching container is intentional, not a test resource leak.
