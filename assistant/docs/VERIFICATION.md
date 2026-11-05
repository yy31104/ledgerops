# Executed verification

The product was exercised locally on 2026-11-05 with disposable PostgreSQL and
synthetic incidents. Generation was fake throughout; the default Compose product
and retrieval evaluation used the real pinned bge-small encoder.

## End-to-end boundaries

| Boundary | Executed check | Observation |
|---|---|---|
| Browser → HTTP | Connect synthetic operator; investigate timeout and terminal blocker | SSE completes, answer and checked API reference visible |
| HTTP → authorization | Real authenticated Gunicorn acceptance tests | Caller scopes enforced; lost reply remains UNKNOWN with 62 held |
| HTTP → dedicated DB | Default Compose initialization and readiness | Packaged migrations/indexing succeed with restricted runtime login |
| UI → graph → checkpoint | Prepare terminal-blocker draft | Exact payload/digest and awaiting-review state visible |
| Process restart → resume | Restart Compose API while paused, then Review & continue | Same action ID and digest reach handoff-ready |
| Resume → draft registry | Query selected action ID after restart | Exactly one persisted draft |
| Handoff → actual proposal API | Isolated real LedgerOps acceptance test; human test driver submits payload | 201 then idempotent 200; authoritative hash differs from draft hash |
| MCP → HTTP → structured response | Actual stdio SDK process and local HTTP fixture | Discovery, structured content and denial paths pass |
| Browser disconnect | Disconnect after handoff | Answer/draft cleared, references reset to zero, session disconnected |
| Responsive panel | 319-pixel and 1280-pixel viewports | Document width equals viewport; no page overflow |
| Browser console | Read warning/error log after investigations/review | No captured warnings/errors |

The browser restart action was `docker compose --env-file assistant/.env -f
assistant/compose.yml restart api`. The reviewed synthetic action remained
`18d3757f-6a57-5671-a89a-10c50e36e63a`; its registry row count was one.
No proposal was submitted through the browser. The separate acceptance test's
explicit human test driver submits only into its own disposable database and
checks that 80 booked / 62 held / 18 available is unchanged. It does not approve
or execute the proposal.

## Reproduce the checks

From the repository root:

```sh
make check
make test
uv run --project assistant --locked ruff check assistant
uv run --project assistant --locked ruff format --check assistant
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml assistant/tests
uv run --project assistant --locked python -m pytest -c assistant/pyproject.toml assistant/tests -m postgres
uv run --project assistant --locked --with-editable . --with gunicorn==26.2.0 \
  python -m pytest -c pyproject.toml -p tests.conftest -q \
  assistant/scenarios/agent_acceptance.py assistant/scenarios/handoff_acceptance.py
npm --prefix assistant/web ci
npm --prefix assistant/web run build
npm --prefix assistant/web test
python3 assistant/scripts/compose.py up
```

The full unchanged core suite passed **1697 tests**. The real API acceptance pair
passed **2 tests**. Dedicated PostgreSQL integration passed **42 tests**, covering
index plans, immutable corpus publication, concurrent draft identity, interrupted
node recovery, changed intent/evidence/expiry, identity rotation and HTTP resume.
After the pending-PR review fixes, default assistant tests passed **105 tests**,
the real-model test passed **1 test**,
and the panel build/type check, **2 stream-parser tests** and **1 reason-validation
test** passed. CI results are
reported on the product PR.

CI runs core checks/tests, baseline snapshot verification, authenticated agent and
handoff acceptance, assistant unit/PostgreSQL tests, real-encoder development
Recall gates, panel build/parser tests and a hashing-encoder Compose smoke test.
Evaluation JSON and MLflow records are retained as CI artifacts for seven days.

## Purposeful failure experiments

1. Change intent bytes while retaining a request/thread ID: the workflow rejects
   reuse rather than replacing the existing draft.
2. Restart before review and resume twice: the action identity and stored payload
   remain unchanged; an opposite repeated decision is rejected.
3. Revoke maker permission, rotate identity, alter evidence or expire the draft:
   continuation fails current authorization/content checks.
4. Script 401, 404, 429, malformed/oversized data and timeouts: the read adapter
   stops, preserves ambiguity or exhausts bounded attempts.
5. Inject write/tool/role instructions: fake-model security tests cannot create
   a write capability or cross the current caller's upstream permissions.
6. Lower development Recall or substitute hashing vectors: the quality-gate tests
   reject the report. Tests do not use held-out values as a tuning objective.

## Remaining evidence limits

The historical v1 holdout was weak: hybrid Recall@5 was 0.3333 versus vectors 0.5000.
The newer frozen retrieval study is documented in
[RETRIEVAL-QUALITY-V2.md](RETRIEVAL-QUALITY-V2.md). Labels remain synthetic and
human-unreviewed. Real-model answer
quality, semantic citation entailment, actual ChatGPT plan usage and production
load/distributed admission were not validated. The local SDK adapter has mocked
contract tests, not an independently verified OS sandbox. No paid inference,
financial execution or real cloud deployment was performed.
