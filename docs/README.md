# Start here

LedgerOps is a reference for testing money-related code. Start with
`make demo-payout-crash` for uncertain payment outcomes and `make demo-lost-update`
for a race hidden by correct totals. Both commands run real PostgreSQL tests;
[the homepage](../README.md) includes their terminal recordings.

## Core and extensions

| Core | Starting point |
|---|---|
| Ledger | [Admission and identity rules](../src/ledgerops/db/admission.py), [schema constraints](../src/ledgerops/db/sql/001_foundation.sql) |
| Payments | [Payout admission](../src/ledgerops/db/payout_admission.py), [evidence resolution](../src/ledgerops/db/payout_resolution.py) |
| Recovery | [Dispatch state](../src/ledgerops/db/payout_dispatch.py), [worker](../src/ledgerops/worker.py) |
| Independent verification | [Verifier](../src/ledgerops/verification.py), [independent journal fold](../reference/journal_fold.py) |
| Concurrent histories | [Recorded reads/writes and offline checker](../tests/history/README.md) |

The HTTP service, authentication, operator console, reconciliation, export, backup
and maintenance extend that core. The operator assistant and deployment recipes
are separate integrations. These guides explain their rules and boundaries.

## Operating guides

| Guide | What you learn |
| --- | --- |
| [Payouts](PAYOUTS.md) | Reserve funds, dispatch a payment, and recover an uncertain outcome |
| [Reconciliation](RECONCILIATION.md) | Compare provider evidence with the ledger and investigate mismatches |
| [Operator actions](OPERATOR-ACTIONS.md) | Propose and review actions under separate identities |
| [HTTP service](HTTP-SERVICE.md) | Run the API and console, inspect requests and responses |
| [Authentication](AUTHENTICATION.md) | Caller tokens, roles, revocation and audit boundaries |
| [Maintenance](MAINTENANCE.md) | Pause, inspect, repair and safely resume |

The [guard mutation guide](MUTATION-TESTING.md) maps each removed protection to
its detecting test. Predict a result before running `make mutant NAME=all`.

The [assistant guide](../assistant/README.md) explains cited answers, read-only
investigation and human-reviewed drafts. The [recovery article](../articles/provider-paid-worker-died.md)
explains the central failure case with a concrete timeline.

## When you need implementation detail

The [internal reference index](internals/README.md) contains wire protocols,
backup/export formats, database invariants, architecture decisions, deployment
recipes and historical readiness checks. These are references, not prerequisites
for trying the demo. Wire identifiers remain documented where compatibility requires them.
