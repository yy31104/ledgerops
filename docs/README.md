# Start here

LedgerOps is a local payment ledger and a read-only operator assistant. Start with
the crash demo in the [project README](../README.md), then choose a guide.

| Guide | What you learn |
| --- | --- |
| [Payouts](PAYOUTS.md) | Reserve funds, dispatch a payment, and recover an uncertain outcome |
| [Reconciliation](RECONCILIATION.md) | Compare provider evidence with the ledger and investigate mismatches |
| [Operator actions](OPERATOR-ACTIONS.md) | Propose and review actions under separate identities |
| [HTTP service](HTTP-SERVICE.md) | Run the API and console, inspect requests and responses |
| [Authentication](AUTHENTICATION.md) | Caller tokens, roles, revocation and audit boundaries |
| [Maintenance](MAINTENANCE.md) | Pause, inspect, repair and safely resume |

The [assistant guide](../assistant/README.md) explains cited answers, read-only
investigation and human-reviewed drafts. The [recovery article](../articles/provider-paid-worker-died.md)
explains the central failure case with a concrete timeline.

## When you need implementation detail

The [internal reference index](internals/README.md) contains wire protocols,
backup/export formats, database invariants, architecture decisions, deployment
recipes and historical readiness checks. These are references, not prerequisites
for trying the demo. Public document names describe their subject; legacy wire
identifiers remain documented where compatibility requires them.
