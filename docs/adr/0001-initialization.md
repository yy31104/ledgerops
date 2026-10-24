# ADR 0001: local foundation and R1 working defaults

Date: 2026-10-23. Status: initialization accepted by task scope; business design
defaults are proposals for subsequent implementation, not individually approved
user decisions. No R1 behavior is implemented by this ADR.

## Context

The [original contracts](../reference/pro-2026-10-23/ledgerops/CONTRACTS.md) put
admission in narrow PL/pgSQL entry points. The [follow-up review](../reference/follow-up-review.md)
supports Python-led SQL, while identifying guarantees that direct writes weaken.
Original sources stay unchanged; this document records the working amendment.

## Foundation

Use separate local `ledgerops` and `chronicle` repositories. LedgerOps starts with
a Python 3.12 package, uv lock, hatchling build, pytest and Ruff. Local initialization
does not add a database, posting implementation, API, provider or deployment.

## Proposed R1 defaults

- Use the platform chart: clearing is debit-normal asset; customer/merchant balances
  are credit-normal liabilities. Use synthetic PLN integer minor units.
- Python coordinates admission and business steps in one explicit transaction with
  hand-written SQL. Retain small database constraints/triggers where needed.
- Lock affected account rows in sorted account-ID order at READ COMMITTED.
- Retain stable workspace-scoped business UUIDs, scoped request keys and terminal
  results, including business rejections, for the retained workspace history.
- Omit reversal from R1; add it in R2. Do not pre-create the source package's
  incorrect all-row unique constraint on `original_operation_id`.

These defaults support a visible Python transaction path, at the cost of trusting
more application code. They are not claims that PL/pgSQL cannot be tested or that
runtime misuse is negligible. Whole admission functions remain a later comparison
option; necessary database protection does not require moving all business flow.

## Trust boundary and changed guarantees

| Authority | Responsibility |
|---|---|
| Database | Explicit amount/identity/scope constraints; complete balanced journals; protected historical immutability; declared per-row balance bounds. |
| Reviewed Python module | Authorization and correct accounts; admission/replay; ordered locking; complete journal and all corresponding projection updates. |
| Independent verification | Journal/projection agreement and business ownership against frozen commands; failure/concurrency evidence. |
| Migration owner | Trusted schema/privilege authority; runtime cannot assume this role. |

A customer funded with 100 buying 80 must retain 20. If Python forgets the customer
projection update, balanced entries and nonnegative balances can still commit.
An independent R1 entry fold must detect it. If both journal and projection credit
M2 instead of requested M1, the fold can agree: a hand-calculated frozen command
must independently assert the intended merchant. Neither check replaces the other.

Append-only rows are insufficient: runtime could append a balanced pair for two
new accounts under an old journal. R1-PR01 must choose the simplest protected
creation/sealing mechanism and prove that later transactions cannot append.
A runtime-reopenable `sealed` flag is insufficient. No mechanism is claimed here
as selected or tested; a narrow protected database primitive is permissible.

Revise `L-DB2`, admission pseudocode and `T-AUTHDB-01` for the direct-SQL boundary.
The future matrix must enumerate allowed INSERT/SELECT/projection updates and
denied historical mutations, TRUNCATE, DDL, role escalation and seal bypass.
The old requirement that every runtime INSERT fails cannot remain an acceptance
criterion. Test with actual runtime credentials, never only the migration owner.

## Verification and stage gates

All transaction, deferred-constraint, permission and lock guarantees need real
PostgreSQL tests. Independent expected values must not import production helpers.
R1 includes the small fold and literal ownership cases; full exports/rebuild stay R2.

R2 must allow multiple rejected reversal attempts but at most one successful full
reversal per original. Replaying a rejected identity stays rejected; improved funds
allow a new identity; concurrent eligible reversals yield at most one success.
Rebuild activation needs a barrier that drains in-flight writers before export and
swap; a mode flag alone is insufficient. External execution requires its own proof.

R5 approvals must bind the current effective evidence head, not only immutable file
hashes/account versions. New relevant evidence invalidates old approval even with
unchanged balances; publication and execution races need a defined lock/CAS rule.

Implementation, verification and personal ownership remain separate records.
Independent project slices may proceed in parallel under their authorized scopes;
human decision packets remain serialized and contain 3–5 grounded decisions.
