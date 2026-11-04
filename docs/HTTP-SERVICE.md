# Authenticated HTTP service and investigation console

Start the service with `python -m ledgerops.service` (or `make service`), using the locked Gunicorn 26.2.0 release.
The service does not create test identities, reset the ledger or run demonstrations automatically.
This page describes the current local interfaces. Controlled approvals are covered by the [operator contract](OPERATOR-ACTIONS.md); Linux deployment and actual restore drills have separate acceptance checks.

## Configuration and startup

An administrator must first create the database and roles, apply migration 010 and issue current credentials as described in AUTHENTICATION.md.
The service login must inherit ledger_app/ledger_reader/ledger_auth_reader. Superusers and members of migrator,
auth_admin or worker roles are rejected. Every database connection checks its roles and the local SQL migration versions and digests. A misconfigured service is not ready.

| Setting | Default or purpose |
|---|---|
| LEDGEROPS_SERVICE_DSN | Required. Keep the secret outside command lines and Git. |
| LEDGEROPS_ENV | production; development permits plaintext debugging only on 127.0.0.1. Authentication remains enabled. |
| LEDGEROPS_TLS_TERMINATED | Must explicitly be true in production, indicating that a trusted TLS entry point has been configured. |
| LEDGEROPS_SERVICE_BIND | 127.0.0.1:8080; production may explicitly use 0.0.0.0 on a private proxy network. |
| LEDGEROPS_SERVICE_HOSTS | localhost,127.0.0.1; at most 16 explicit hostnames, without wildcards. |
| LEDGEROPS_TRUSTED_PROXIES | 127.0.0.1,::1; at most 16 explicit proxy IP addresses. Arbitrary sources cannot be trusted. |
| LEDGEROPS_SERVICE_WORKERS | 2, with a range of 1–4; each worker handles one request synchronously. |
| LEDGEROPS_RESTORE_MODE | true; financial writes and callbacks are rejected while enabled. |
| LEDGEROPS_API_WRITES_ENABLED | false; writes require explicit true and restore mode false. |
| LEDGEROPS_PAYOUT_POLICY_FILE | Optional owner-private regular JSON file; new payouts are rejected when it is absent. |
| LEDGEROPS_CALLBACK_DSN/NAMESPACE/KEYS_FILE | Optional, but all three must be configured together. Uses a separate worker account and signing trust; no callback endpoint is available otherwise. |
| LEDGEROPS_OPERATOR_DSN/OBJECTS/NAMESPACE/KEYS_FILE | Optional, but all four must be configured together. Uses a separate operator account, object storage and signing trust; no operator endpoint is available otherwise. |

Production business pages and APIs require HTTPS. Gunicorn accepts X-Forwarded-Proto:https only from configured proxy IP addresses;
a header supplied by an ordinary client cannot establish identity or TLS. Do not expose the plaintext backend port to the public internet.
TLS_TERMINATED is an explicit deployment requirement, not evidence that the service has checked certificates or domain configuration.

Plaintext local development requires explicit `LEDGEROPS_ENV=development` and a loopback address. It still requires real database credentials and an issued token.
It neither disables authentication nor injects fixtures. Restore and write flags are not inferred from database NORMAL mode, so restoring an old backup cannot automatically reopen writes.

The payout policy file contains exactly `version:1` and a `workspaces` array. Each entry contains workspace_id, provider_namespace,
fixed_fee_minor, dispatch_ttl_seconds and beneficiary_owners (beneficiary UUID → owner actor UUID).
Fees and beneficiary ownership come from the administrator's file; a request body cannot replace that policy. Symlinks, FIFOs,
files owned by another user and group/world permissions are rejected. The file is limited to 64KiB, with at most 128 workspaces and 1000 beneficiaries per entry.
Signing key files use the worker's private-file checks and allow at most eight keys.

## API

All /v1 business endpoints use `Authorization: Bearer <token>`. Workspace, client and actor identities are derived from the credential.
Financial POST requests also require `Idempotency-Key`. At the HTTP boundary, it must contain 1–128 ASCII letters, digits or `._~-`.
This is narrower than the internal Python RequestKey character range, preventing merged duplicate headers from creating an ambiguous new identity.
Bodies must be strict UTF-8 JSON. Duplicate fields, non-finite numbers and unknown business fields are rejected; amounts must be decimal strings in minor units.
The outer body is limited to 16KiB. Content-Length is required and Transfer-Encoding is not accepted. Credentials and transport retry details are excluded from business identity.

| Route | Current permission or result |
|---|---|
| GET /health/live | No credential required; reports process liveness and does not fail because of a brief database outage. |
| GET /health/ready | Checks actual roles, SQL capabilities and deployment flags; returns 503 when not ready, without secrets or a workspace list. |
| POST /v1/purchases | purchase:write; 201 success or 422 business refusal, with a PurchaseCommand body. |
| POST /v1/reversals | reversal:write; full reversal by the original merchant, with a ReversalCommand body. |
| POST /v1/payouts | payout:write; matching policy yields 202 acceptance and a hold; 202 is not payment success. |
| GET /v1/payouts/UUID | payout:read; checks original merchant ownership and reads current state and evidence references from one snapshot. |
| GET /v1/operations/UUID | operations:read; original receipts only for operations submitted by this client; the GET 200 body retains the original http_status. |
| GET /v1/me | Current valid credential; exposes only public identity, effective scopes, expiry and operator-panel availability. No account-read permission is required. |
| GET /v1/accounts?after=UUID | accounts:read; only accounts owned by this actor, 100 per page with explicit next_after. |
| GET /v1/investigation/operations/UUID | evidence:read; workspace investigation with original request digest, entries, hold and recent evidence references. |
| GET /v1/metrics | evidence:read; workspace pending counts and ages, BLOCKED dispatch records and quarantine counts. |
| /v1/operator/actions and subroutes | Enabled only with separate configuration; proposal, approval and execution each have fixed scopes. See the operator contract. |

Investigation responses contain at most 100 entries, 20 observations and 32 events, with explicit have_more flags. Recent records are not a complete history.
Financial amounts, versions, attempt counts and external source sequence numbers are strings to avoid browser integer precision loss. Counts, row numbers and HTTP statuses are small integers.
Internal consistency, a retained digest or a 202 response does not establish that an external payment completed. UNKNOWN/ACCEPTED states still require investigation.

401 means no current credential; 403 means insufficient scope; 404 conceals unauthorized objects; 409 means an identity/content conflict;
422 is a stable business conclusion; 503 means temporary unavailability, restore access still closed or possible recovery using the original identity.
A disconnect can occur after COMMIT: retries must retain the original operation ID and Idempotency-Key rather than create a new business identity.
The core retries only definitely aborted 40P01/40001 transactions. The HTTP layer does not automatically retry transactions with unknown outcomes.

## Callbacks

With complete configuration, `POST /v1/provider/callbacks/<workspace UUID>/<payout UUID>` accepts the existing provider-event:v1
signed envelope rather than an ordinary user Bearer token. It verifies the HMAC domain over the exact payload string before connecting to the database.
An invalid signature returns 401 without looking up the target or writing quarantine records. A valid signature still goes through the complete payout identity,
amount and namespace binding, evidence retention, contradiction blocking and one-time settlement checks. Reordering the outer JSON does not change the signed payload bytes;
verification does not regenerate business content and sign that replacement. Acceptance returns 202, with status distinguishing APPLIED, REPLAYED, INCIDENT, BLOCKED and other outcomes.
Receiving a callback does not unconditionally establish payment success. The restore deployment flag also blocks callback financial writes.
Callback connections use only the separate worker role; the ordinary service role receives no additional privileges.

## Investigation console

The root path `/` serves the bundled investigation console: identity connection, accounts, investigation by business UUID and pending-state queries.
Current reconciliation run/epoch references come from the same authorized snapshot, with at most 20 entries and an explicit have_more flag. These references do not establish that object files have been verified.
A separate operator instance also provides proposal → another actor reviews the exact original text → execution. Controls are enabled only when both configuration and current scopes allow them.
Credentials live only in page memory, and the input clears on connection. Logout, refresh and navigation clear the credential. Session generations prevent old asynchronous responses
from redisplaying data after logout. The UI uses textContent and does not execute HTML from records; it has no external scripts, browser persistence or operation callbacks.
The original `proposal_json` string preserves canonical bytes. The browser verifies SHA-256 and binds approval to that digest, avoiding large-integer drift from parsing and serializing again.
Static assets have only three fixed paths; arbitrary file reads are rejected. CSP, same-origin restrictions, nosniff and no-store headers are defaults.
Browser automation was disabled by the Work settings during this delivery. Actual HTTP assets/API checks and Node syntax checks passed; visual appearance and real-browser interaction still require acceptance.

## Resource limits and shutdown

There are 1–4 synchronous workers and a backlog of 64. Request lines are limited to 4094 bytes, with at most 32 headers of 2048 bytes each.
Each request owns only its own connection. Limits are 3 seconds for connection, 10 for statements, 5 for locks, 30 for transactions and 15 for idle transactions.
Gunicorn allows 40 seconds per request and 45 seconds for graceful shutdown. SIGTERM stops new requests and allows bounded in-flight work to finish.
Startup emits actual worker initialization events for monitoring. The source does not create unbounded per-request threads, an unbounded connection pool or in-process background payment tasks.

JSON request logs contain the server request ID, fixed route/status and duration, plus operation/action/execution/observation correlation IDs from successful results after UUID validation.
They do not contain arbitrary request bodies or paths. Raw access logging is disabled. HTTP parsing warnings and exceptions use fixed events to avoid leaking headers, URL queries, DSNs, payloads and keys.
Parser-generated 400/431 responses before the application may use the server's standard error pages.
Metrics read the currently authorized workspace database; per-worker memory counts are not presented as global totals.
Workspace export and evidence capacity limits remain those in the reconciliation documentation. This slice makes no production-throughput, high-availability or arbitrary-capacity guarantee.

The main implementation references are the installed Gunicorn 26.2.0 source, its [official settings](https://gunicorn.org/reference/settings/)
and the [locked release](https://pypi.org/project/gunicorn/26.2.0/). Database deadlines were checked against an actual PostgreSQL 18 server.
