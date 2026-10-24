# R1-PR02 local verification — 2026-10-23

Final result: **221 passed; 0 failures, errors or skips**, 10.137 seconds.
JUnit start: `2026-10-23T18:45:58.957899+02:00` (Europe/Warsaw).
This extends the earlier PR01 foundation evidence with actual purchase admission.
It is not complete R1 contention or recovery acceptance.

## Runs and results

| Executed check | Actual outcome |
|---|---|
| `UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R1-PR02/2026-10-23/pytest.xml` | Final 221 passed: 91 domain unit, 18 independent reference, 112 real PostgreSQL integration. |
| Initial purchase/atomicity/barrier run | 40 passed before the extra precision and retained-integrity cases were added. |
| First expanded full run | 210 passed, 11 failed during corruption-fixture setup. Preserved in `pytest-initial.xml`; not counted as acceptance. |
| Targeted retained-integrity rerun | 13 passed after fixing fixture trigger restoration. |
| `make check` | Ruff clean, 31 Python files formatted; non-editable package imports and migration SQL resource found. |
| `make build` | Wheel and sdist built. Wheel admission and SQL bytes match current sources; sdist includes tests/reference/fixture, excludes private references. |
| Reference/fixture digests | All 17 original reference hashes and frozen purchase fixture hash unchanged. |
| Cleanup observation | Docker query for `ledgerops.scope=disposable-test` returned no containers after the final suite. |

The initial 11 failures were PostgreSQL ObjectInUse errors while test-only owner SQL
tried to restore transition triggers with deferred terminal events still queued.
The fixture now executes `SET CONSTRAINTS ALL IMMEDIATE` before restoring each guard.
CHECKs, foreign keys and terminal checks stay enabled. This changes test preparation,
not production protections. The final full run includes all previously failing cases.

## New evidence: 56 integration tests

- 31 purchase-service tests cover the literal 100/80 scenario, exact journal/result
  binding, unchanged same-key replay, new-key/client association, payload conflicts,
  rejected101→fund100→old rejection/new business acceptance, disabled-account policy,
  authorization on incoming and retained commands, scope separation and explicit M2
  selection. Limits include maximum operation amount, cumulative/version exhaustion,
  reserved availability and 38-digit totals with Decimal precision deliberately set
  to 6. Extreme projection fixtures test arithmetic bounds, not a complete historical fold.
- 7 atomicity tests inject real SQL errors after journal entries, the second projection,
  operation finalization, request finalization and during deferred COMMIT. Assertions
  require the exact injected constraint error and a wholly unchanged database snapshot.
  Removing the fault allows the same identities to succeed, then replay. Existing
  transactions and non-autocommit connections are refused before service writes.
- 5 barrier tests cover NORMAL, FROZEN and RESTORE, exclusive maintenance preceding
  a writer, and maintenance waiting on an in-flight writer's shared barrier. Actual
  PostgreSQL blocking relations establish ordering. Queued writers have no financial
  write locks before barrier admission; timeout is not an acceptable substitute.
- 13 retained-integrity tests cover ACCEPTED with no journal, request/operation
  disagreement, invalid canonical bytes with a correct digest, and JSON booleans
  masquerading as numbers. They require precise invariant failures and no new-key
  residue. Owner corruption restores mutation guards before replay testing.

The 56 existing PR01 PostgreSQL tests, 91 domain tests and 18 independent-reference
tests also pass. The reference fold remains independent of production money/posting
helpers. Frozen original-owner expectations supplement balance equality.

## Environment, source and artifacts

Same local toolchain as PR01: Python 3.12.13, uv 0.11.14, pytest 9.1.1, Ruff 0.16.9,
hatchling 1.32.4, psycopg/psycopg-binary 3.3.6, libpq 180006, macOS 27.0.1 arm64,
Docker client/server 29.4.3. PostgreSQL server_version_num 180006 is asserted by setup.

Image: `postgres@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`.
Tests use actual runtime login credentials, random passwords, an ephemeral localhost
port and fresh databases inside their own disposable tmpfs container. No existing
application database is touched. Missing Docker fails the suite.

The [source manifest](SOURCE-SHA256SUMS) records 45 source/test/config/document paths
relative to the repository root. The [artifact manifest](ARTIFACT-SHA256SUMS) records
[final JUnit](pytest.xml), [initial failed-setup JUnit](pytest-initial.xml) and local
wheel/sdist hashes. No production Python/SQL/test bytes changed after the final run;
subsequent handoff/evidence changes are documentation only. There is no commit hash:
Git is local `main` with no commits or remote. PR01's manifest is historical, not a
promise that its paths still contain the older bytes after PR02.

Frozen fixture SHA-256:
`3f2cdab26da61a95bf5a7648dccafffe18fd0a67af7f8f0b03892132784091a3`.

## Practical limits

Principal comes from a trusted caller; no token verification or HTTP adapter exists.
The runtime credential can bypass service rules. Replay validates specific canonical,
response and journal-header relationships, not all historical lines and projections.
Account administrative changes must participate in the exclusive workspace barrier.

The broader simultaneous-debit/identity race suite, automatic whole-transaction retry,
lock/cancellation recovery and lost-reply/unknown-COMMIT experiments remain PR03.
PR02 has no internal transaction retry loop. Connection loss can leave the outcome
unknown and must never cause a new business identity to be invented. No external
provider, reversal, hold lifecycle, deployment, tracker change or personal-mastery
claim is part of this delivery.
