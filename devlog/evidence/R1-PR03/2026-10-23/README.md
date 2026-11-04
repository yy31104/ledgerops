# R1-PR03 local verification — 2026-10-23

Final result: **237 passed; 0 failures, errors or skips**, 29.415 seconds.
JUnit start: `2026-10-23T20:16:17.698425+02:00` (Europe/Warsaw).
This completes the PR03 contention/retry/recovery task for its bounded schedules
and fault locations. It does not claim production readiness or every possible failure.

## Executed checks

| Check | Actual outcome |
|---|---|
| `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R1-PR03/2026-10-23/pytest.xml` | 237 passed: 91 unit + 18 independent reference + 128 real PostgreSQL integration. |
| Targeted concurrency | 6 passed, 2.26 seconds. |
| Targeted recovery + retries after proxy cleanup repair | 9 passed, 10.25 seconds; the extra deferred-COMMIT retry variant was added afterward and is covered by the final suite. |
| Earlier complete run after deferred-COMMIT variant | 237 passed, 18.43 seconds; final rerun binds the final SQL-string formatting and current source manifest. |
| `make check` | Ruff clean, 36 Python files formatted; installed package imports and SQL resource found. |
| `make build` | Wheel and sdist built; current admission and SQL bytes match wheel contents. |
| Built-wheel import | Imported directly from the generated wheel; actual retry wrapper and migration SQL resource found. |
| Archive inspection | sdist includes retry tests, wire-fault proxy, independent reference and frozen fixture; private source documents excluded. |
| Reference/fixture hashes | 17 preserved source documents and the frozen purchase fixture retain their original digests. |
| Resource cleanup | `docker ps -a --filter label=ledgerops.scope=disposable-test` returned no containers after the final suite. |

## Sixteen new integration cases

Six concurrent schedules run actual service calls using separate runtime connections:

- Two different purchases of 80 from customer 100 yield exactly one 201 and one
  retained insufficient-available 422; customer 20, M1 80, one purchase journal.
- Same request identity and new-key/same-business identity wait on the winning
  uncommitted claim, then replay its committed result from a fresh statement snapshot.
- Changed payloads under either identity wait, then conflict 409 without another
  effect or orphan new-key reservation.
- A lower merchant UUID and higher customer UUID demonstrate ascending ID locks:
  while the merchant lock blocks admission, a separate NOWAIT customer probe succeeds.

Seven retry/cleanup cases drive PostgreSQL rather than mocked admission results:

- SQLSTATE 40001 injected during writing and deferred COMMIT fails the first two
  attempts, then succeeds on the third. Nontransactional sequence values and notices
  record distinct transaction/journal IDs, unchanged operation/payload/key, and each
  attempt's complete one-operation/one-journal/two-line 20/80 intermediate state.
- Three 40001 failures exhaust the budget. Only two backoffs occur; the last original
  database error is retained as cause, and no financial or identity effect survives.
- An injected 23514 integrity violation propagates without retry. Removing the test
  fault allows the original identities to succeed.
- A deliberately constructed row/advisory-lock cycle produces an actual 40P01
  deadlock in the runtime backend, then one successful whole-transaction retry.
- Actual query cancellation 57014 and actual 5-second lock timeout 55P03 each produce
  an unretained retryable error with no internal backoff, IDLE connection, unchanged
  database state and released shared workspace barrier. Unlocking permits same-
  identity success. Timeout is an intended outcome here, not proof of a race schedule.

Three process/wire recovery cases observe real failure points:

- A spawned runtime caller completes the service COMMIT, signals a control channel,
  and is SIGKILLed before a distinct business-reply channel is released. That channel
  has no reply. A new connection retrieves the original result.
- A single-connection loopback proxy observes frontend Q COMMIT but forwards zero
  such frames before disconnecting. Independent state proves actual rollback; same-
  identity recovery creates exactly one effect.
- The proxy forwards COMMIT, receives backend C COMMIT, forwards zero completions,
  and disconnects. The caller receives actual OperationalError, while a new observer
  proves the committed result. Same-key and new-key/same-operation recovery replay it.

Both wire faults look like connection failure to the caller. Test observers know
which protocol event was withheld; application logic does not infer the outcome
from the exception. The proxy forwards startup/authentication frames opaquely and
records only COMMIT events/counters, never credentials or wire payloads.

All final-money cases retain the independent raw entry fold and frozen original-
owner/amount expectations. The preceding 221 PR01/02 tests also remain green.

## Verification issue found and corrected

Initial three-case recovery runs had two passes and one failure: the before-forward
proxy relay did not exit within its watchdog. An isolated rerun could pass, so that
was not treated as resolution. Faulthandler showed the backend relay still inside
`recv` after the opposite thread closed the connection. Serializing close calls alone
did not resolve it.

The proxy now has cooperative stop checks with short socket read timeouts, a real
read deadline, one shutdown owner, and explicit cleanup of sockets established while
shutdown is racing. EOF/OS errors after deliberate shutdown are expected, while
protocol/fault assertions still require the exact frontend/backend COMMIT events.
The corrected combined recovery/retry run and both complete runs passed. No production
posting guard or result assertion was weakened to make the tests pass.

## Environment and byte identity

- Python 3.12.13, uv 0.11.14, pytest 9.1.1, Ruff 0.16.9, hatchling 1.32.4.
- psycopg / psycopg-binary 3.3.6; linked libpq 180006; macOS 27.0.1 arm64.
- Docker client/server 29.4.3; PostgreSQL server_version_num 180006 asserted by setup.
- Image: `postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`.
- Random credentials, ephemeral localhost-only port, tmpfs cluster, fresh database
  per test. Actual `ledger_app` credentials execute admission. Owner/admin SQL is
  limited to fixture setup, observation and declared test faults. The deadlock timer
  is set by admin for the test database; runtime receives no privilege escalation.
- The TCP fault proxy disables encryption only on its disposable localhost test
  connection so actual protocol frames can be inspected. It is not product code.

[Source manifest](SOURCE-SHA256SUMS) binds 57 source/test/config/document paths relative
to the repository root. [Artifact manifest](ARTIFACT-SHA256SUMS) binds [JUnit](pytest.xml)
and local wheel/sdist files. Production/test bytes did not change after the final
run. The evidence prose was written afterward. No Git hash is invented: local
`main` still has no commits or remote. Older PR01/02 manifests describe historical
bytes and may not match paths changed by subsequent slices.

Frozen fixture SHA-256:
`3f2cdab26da61a95bf5a7648dccafffe18fd0a67af7f8f0b03892132784091a3`.

## Scope and next work

Only 40P01/40001 retry, at most three total attempts; sleep happens after rollback,
with an open IDLE connection and unchanged identities. Connection loss is propagated
without automatic reconnect or retry. The caller owns reconnection and resubmission;
there is no durable command archive or production client wrapper in this slice.
SQLSTATE 40001 is injected for retry testing, not claimed as natural Serializable
conflict under READ COMMITTED. The deadlock is a real PostgreSQL deadlock.

The three fault locations and controlled schedules are finite evidence, not exhaustive
proof. Token verification/HTTP, persistent demo database, maintenance APIs, reversal,
external provider and durable operational packaging are separate tasks. R2 independent
checking/export/rebuild/reversal is the next source release and has not begun here.
Implementation, verification and personal ownership remain separate in the
[handoff](../../../HANDOFF.md). All work stays local; no push, deployment or tracker change.
