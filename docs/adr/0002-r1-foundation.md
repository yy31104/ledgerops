# ADR 0002: executable R1 foundation

Date: 2026-10-23. Status: local implementation; first four business defaults accepted.

## Context and authority

Yi authorized R1-PR01 implementation and accepted [packet 1](../DECISION-PACKET-R1.md).
The latest Vault LedgerOps working blueprint rev 1 supplies Python-led admission,
immutable header declarations, column-level runtime grants, R2 reversal/drain and
R5 evidence epochs. Its design-only status is historical; it is not a current test result.
The preserved Pro package and reviews under `docs/reference/` remain unchanged.

## Decisions implemented in PR01

- PostgreSQL 18.6, immutable image digest recorded in the test fixture; psycopg 3.3.6.
  Retain the already initialized Python 3.12 interpreter. Use explicit, ordered SQL
  migrations packaged with the Python code and recorded by content checksum. A small
  transaction runner is sufficient for this one migration; no ORM or Alembic added.
- `ledger_migrator` is a non-login schema/database owner in the disposable fixture.
  `ledger_app` is a separate actual login, with no owner/superuser/escalation privileges.
  Python owns future business admission; there is no SECURITY DEFINER workflow.
- Header count (2–16) and one-sided total are immutable. Deferred header **and**
  entry triggers compare the final complete journal at commit. Historical balanced
  additions increase its count/totals and fail. No writable seal flag or transaction-ID trick.
- Pending operation/request rows can exist only inside a transaction. Deferred
  checks query their final row by identity, rather than the queued trigger's stale
  `NEW` tuple. Terminal updates, including no-op updates, are rejected.
- Totals use a `numeric` domain without scale coercion, checking integer equality
  and the range 0 through 10^38−1. This corrects rev 1's `numeric(38,0)` example:
  a typmod can round a fractional value before CHECK; NaN also needs an upper bound.
  `booked` is generated from valid debit/credit totals and immutable normal side.
- Database/public-schema creation, PUBLIC temporary-table privilege and PUBLIC
  ledger-function execution are revoked. Trigger functions use invoker rights,
  fixed search paths and qualified references. Runtime cannot disable them.
- The clearing account may be owned by the fixture's SYSTEM actor, as allowed by
  the source's “workspace/system” convention. Frozen expected data was not changed
  to satisfy an unnecessarily narrower database assumption.

## Input and identity details

UUID text is lowercase and hyphenated; no specific UUID version is required.
Wire amounts are canonical unsigned decimal strings; positive operations are at
most 9e15 minor units. Boolean, float, exponent, sign and leading-zero inputs fail.
Request keys retain their codepoints/case/whitespace, occupy 1–128 UTF-8 bytes, and
exclude NUL. Typed client, actor, account, workspace and operation IDs are distinct.

Canonical purchase v1 is `ledgerops.purchase:v1\n` followed by compact UTF-8 JSON
in order: workspace, operation, customer account, merchant account, amount string,
currency (exact field names are frozen in the literal unit-test vector). Request
keys/client credentials/generated timestamps are excluded. Compare stored bytes;
SHA-256 is an integrity/index aid. Duplicate JSON keys need rejection by the future
HTTP decoder; PR01 accepts an already-decoded mapping and does not implement HTTP.

## Trust and evidence

See [working contract](../working/R1-CONTRACT.md) and the
[task](../tasks/R1-PR01.md). Database constraints do not establish correct merchant,
request amount, journal/projection correspondence, or actor authorization.
Real runtime SQL deliberately demonstrates these gaps; independent checks detect
the missed projection and consistent wrong-merchant defects after they commit.

SQL `bigint` constrains stored entry values, not the pre-cast SQL expression:
an explicit numeric-to-bigint cast can round. Python validates the wire domain.
The runtime is a trusted service credential with cross-workspace SELECT access;
composite foreign keys establish referential scope, not row-level authorization.

The small reference fold is test evidence over posted entries, not the R2 export,
hold lifecycle, drift service or rebuild implementation. Test fixtures write explicit
SQL and are not a production admission API. Real purchase replay, response loss,
debit concurrency and the workspace barrier belong to PR02/03.

## Primary semantics checked

- [PostgreSQL numeric values and coercion](https://www.postgresql.org/docs/18/datatype-numeric.html)
- [Deferred constraint timing](https://www.postgresql.org/docs/18/sql-set-constraints.html)
- [Default privilege scope](https://www.postgresql.org/docs/18/sql-alterdefaultprivileges.html)
