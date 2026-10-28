# Local learning page verification — 2026-10-28

Base: `60c4f2f51e097e263754c9f1980febfbf6595351`. Locate the containing delivery
commit with `git log -1 --format=%H -- evidence/LAB/2026-10-28/verification.json`.
This is a single local slice; no push, remote, deployment or tracker update.

## Results

- Final complete regression: **595 passed in56.10s**, zero failures/errors/skips.
  272 unit +41 reference +282 integration cases. See `pytest.xml`.
- After clarifying that replay targets the latest attempted reversal, added an
  assertion that its retained422 is replayed without a new operation; all6HTTP
  tests pass in10.75s. See `lab-http-final.xml`. This was a test/copy clarification,
  not a service or adapter behavior change.
- Six real-PG tracing tests cover actual command order/source/role/parameters,
  statement failure+ROLLBACK, deferred COMMIT failure, suspend/nested capture and
 40001 write/COMMIT retry. No inferred server-internal SQL is added to the trace.
- HTTP tests run all12 guided steps, fresh-workspace reset followed by correct
  guide_step attribution, M2 freeplay, replay/conflict409, new duplicate422,
  input/origin/host/token limits, every static asset and all source excerpts.
- Subprocess `make lab` smokes run the story via HTTP and exit by the end button
  and by Ctrl-C. Before/after container sets match; temporary exports are removed.
  Ctrl-C can interrupt make/uv with the conventional nonzero exit; cleanup, rather
  than an assumed zero exit, is asserted. Docker-unavailable startup fails clearly.
- `make check`, `make build`, `node --check lab/app.js`, `git diff --check` pass.
  Both wheel and sdist exclude lab implementation/assets and lab-only tests.
  Production files/migrations, original fixtures/reference and historical evidence
  match the base. No runtime dependencies were added.

## Review and limits

Session and trace were independently reviewed by subagents. Lesson review corrected
workspace-scoped uniqueness, healthy/reservation semantics, owner role usage,
preview-ID limits, trace boundaries and actual source references. Root additionally
clarified most-recent-attempt replay and fixed event-to-lesson attribution after reset.
The complete12-lesson/19-glossary copy is in `docs/tasks/LAB.md`.

Browser creation returned **“Local browser use is disabled in Work settings”**.
No automated browser clicks, screenshots or visual-layout claims are included.
HTTP/asset/source checks and JS syntax/static-control checks do not replace visual
validation. The page is opened for the user via the app's browser-panel tool.

Development first exposed two incorrect test assumptions: autocommit drift needn't
issue explicit BEGIN/COMMIT, and Ctrl-C needn't give make exit0. Tests were corrected
to assert actual client commands and actual cleanup. No core behavior was weakened.

The final interactive session is intentionally left running with an empty workspace;
its single disposable container is not a test leak. End it using the page button or
Ctrl-C. Closing the browser tab alone does not stop it. Concurrency single-step
visualization and individual learning mastery are not part of this slice.
