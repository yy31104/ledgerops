# ADR 0001: application-led ledger foundation

Date: 2026-10-23. Status: accepted as the foundation; later ADRs record the implemented transaction and retry details.

## Context

Moving admission from narrow PL/pgSQL entry points into Python makes business flow
and transaction boundaries visible in the application. It also trusts more runtime
code, so database constraints and independent verification have distinct responsibilities.

## Foundation

LedgerOps is a Python 3.12 package with a uv lockfile, Hatchling build, pytest and
Ruff. Chronicle is maintained in a separate repository.

## Ledger defaults

- Use the platform chart: clearing is debit-normal asset; customer/merchant balances
  are credit-normal liabilities. Use synthetic PLN integer minor units.
- Python coordinates admission and business steps in one explicit transaction with
  hand-written SQL. Retain small database constraints/triggers where needed.
- Lock affected account rows in sorted account-ID order at READ COMMITTED.
- Retain stable workspace-scoped business UUIDs, scoped request keys and terminal
  results, including business rejections, for the retained workspace history.
- Full reversals use a separate operation identity. Only successful reversals are
  unique per original operation, so rejected attempts do not prevent later eligible attempts.

These defaults support a visible Python transaction path, at the cost of trusting
more application code. They are not claims that PL/pgSQL cannot be tested or that
runtime misuse is negligible. Whole database-side admission functions are an
alternative architecture; this design keeps business flow in explicit Python transactions.

## Trust boundary and changed guarantees

| Authority | Responsibility |
|---|---|
| Database | Explicit amount/identity/scope constraints; complete balanced journals; protected historical immutability; declared per-row balance bounds. |
| Reviewed Python module | Authorization and correct accounts; admission/replay; ordered locking; complete journal and all corresponding projection updates. |
| Independent verification | Journal/projection agreement and business ownership against frozen commands; failure/concurrency evidence. |
| Migration owner | Trusted schema/privilege authority; runtime cannot assume this role. |

A customer funded with 100 buying 80 must retain 20. If Python forgets the customer
projection update, balanced entries and nonnegative balances can still commit.
An independent journal fold must detect it. If both journal and projection credit
M2 instead of requested M1, the fold can agree: a hand-calculated frozen command
must independently assert the intended merchant. Neither check replaces the other.

Immutable header declarations fix each journal's entry count and one-sided total.
Deferred checks compare the complete journal at commit, preventing a later
transaction from appending another balanced pair under an old header.

Runtime privileges permit the inserts and projection updates needed for admission,
while denying historical mutations, TRUNCATE, DDL and role escalation. Integration
tests exercise those privileges with runtime credentials rather than the migration owner.

## Verification and recovery

All transaction, deferred-constraint, permission and lock guarantees need real
PostgreSQL tests. Independent expected values must not import production helpers.
The small journal fold and literal ownership cases check isolated postings.
Complete exports and projection rebuilds have separate verification paths.

Reversal admission allows multiple rejected attempts but at most one successful full
reversal per original. Replaying a rejected identity stays rejected; improved funds
allow a new identity; concurrent eligible reversals yield at most one success.
Rebuild activation needs a barrier that drains in-flight writers before export and
swap; a mode flag alone is insufficient. External execution requires its own proof.

Operator approvals bind the current effective evidence head, not only immutable file
hashes/account versions. New relevant evidence invalidates old approval even with
unchanged balances. The [operator lock order and invalidation checks](../OPERATOR-ACTIONS.md#locks-invalidation-and-resource-limits)
define the current serialization rules for publication and execution.
