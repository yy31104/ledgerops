# LedgerOps — engineering handoff

**Date:** 2026-10-23. **Actual state:** design and source research only. There is no implementation repository, code commit, database, deployment, passing test or benchmark for this project. No project files on the user's Mac, tracker or reminder were modified by this research.

## Goal and accepted constraints

Build a synthetic marketplace transaction ledger, external-outcome recovery and independent reconciliation system. LedgerOps and Chronicle are selected directions. No real money, broker/card integration, employer material or paid service. AI may implement multiple releases ahead of personal learning; implementation, verification and personal ownership remain separate. Explain 3–5 consequential choices before each release. Detailed defaults below are recommendations awaiting review and user decisions.

## Recommended architecture

Python/FastAPI, explicit psycopg/PostgreSQL transactions, SQL-first migrations and protected posting entry points; an independent provider process with a SQLite store; an independent reference model; a narrow React operations UI later. Single synthetic PLN, exponent 2; API minor amounts are strings. Customer/merchant liabilities are credit-normal; the clearing asset is debit-normal. A hold reduces availability without posting money. Pending funds and unknown external outcome are independent dimensions.

Provider keys and terminal tombstones persist for the namespace lifetime. Timeout, 500 and lagging NOT_FOUND never release a possibly sent hold. A synthetic `close-if-absent` operation supplies explicit negative finality; real providers are not assumed to have it. Restore disables dispatch, preserves logical identities and compares provider history with independently retained commands and request history; missing evidence remains a reported gap.

## Read next

- [CONTRACTS](CONTRACTS.md): accounting, schema/privileges, atomic posting, replay, provider states, publication and worked traces.
- [VERIFICATION](VERIFICATION.md): requirements, independent oracles, failpoints, mutations and workloads.
- [IMPLEMENTATION-AND-DECISIONS](IMPLEMENTATION-AND-DECISIONS.md): reviewable PRs and release decisions.
- [Reference register](../01-REFERENCE-REGISTER.md#l-01): exact source revisions, licenses and transfer limits.

## First actionable task

Review the chart, identity retention, row-lock strategy and DB write boundary. Then implement R1-PR01: typed money/identity contracts; first migration and runtime privileges; literal opening-100/purchase-80 fixtures; a real PostgreSQL test rejecting an incomplete journal. R1-PR02 adds posting and persisted replay; R1-PR03 adds concurrency and lost-reply tests. Chronicle can start independently; no “LedgerOps R1 first” gate is assumed.

Future entry points, **not existing commands**: `make bootstrap`, `make db-up`, `make migrate`, `make test-unit`, `make test-integration`, `make test-contracts`, `make evidence RELEASE=R1`.

## Open decisions and evidence limits

Decide platform chart versus wallet abstraction; protected DB functions versus application-only writes; row locks versus conditional updates/SERIALIZABLE; identity retention; full-reversal scope. Runtime candidates need exact patch, digest and compatibility review. Public source code, tests and licenses were inspected; no reference code was executed. Fixed-seed inputs do not make PostgreSQL scheduling deterministic. Source guarantees do not transfer automatically.

**Do not touch:** old projects, employer/private repositories, thesis/CV/tracker/reminders, real payment services or cloud publication. Do not copy Formance v3 code assuming v2's license: current v3 reuse rights were not established. External deployment, accounts, API costs and publication require separate authorization. Desired outcomes and workload sizes are not achieved results.
