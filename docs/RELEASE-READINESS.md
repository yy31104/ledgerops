# LedgerOps release and operating guide

The project has grown from a single posting example into a service with authentication, asynchronous payouts, independent verification, controlled operator actions and recovery tools.
The runtime package still uses a synthetic provider and has been exercised in local Linux containers. Local tests do not establish real-money handling, bank finality or cloud operation.
Recorded commands, test results, image identities and cleanup outcomes are in the
[delivery acceptance record](../devlog/evidence/DELIVERY/2026-10-29/verification.json).

## Delivered flows

| Area | Capability | Boundary |
|---|---|---|
| R1/R2 | Purchases, concurrent overdraft prevention, original-identity retries, reversals, A/B verification, draining writers and projection rebuilds | SQL constraints alone do not establish correct balances or ownership; independent checks detect drift. |
| R3 | Persistent payout instructions, holds, conservative dispatch, authenticated observations, unknown-outcome recovery and durable closed-payment records | 202 means acceptance only; elapsed time or NOT_FOUND cannot release a hold. |
| R4 | Fixed reconciliation sources, complete group matching, immutable runs, pinning every block and atomic current publication | Reconciliation finds differences without repairing balances automatically; incomplete sources cannot produce a healthy result. |
| R5 | Current-permission authentication, HTTP service, investigation console, distinct-actor approvals and backup recovery | Stale permissions, evidence and versions invalidate authorization; restore defaults to no financial writes or dispatch. |
| R6 | Selected synthetic evidence and sources, strict versions/units and an independent standard-library reader | Read-only, with no database credentials or operator repair capability; the Chronicle application is not integrated into this repository. |
| Operations | Pinned images/dependencies, non-root containers, TLS, role separation, resource limits and graceful shutdown | Local single-host scope; an actual deployment needs its own identities, network and provider contract. |

Using the same amounts throughout: a customer with 100 purchases 80, leaving customer 20 and merchant 80. A merchant payout of 60 plus a fee of 2 reserves 62,
leaving 18 available. Only authenticated SUCCEEDED consumes 62 and posts local entries. A negative terminal result releases the hold; UNKNOWN retains it.
If the provider pays and the local process crashes, recovery reads existing external evidence and settles under the original identity without paying again.

## Running the project

Source development and tests require Python 3.12, uv and Docker:

```sh
make bootstrap
make check
make test
make build
```

The first run may download locked dependencies and the PostgreSQL image. Integration tests fail explicitly when Docker is unavailable.
The demo/lab subprocesses inside `make test` explicitly reuse the running pytest interpreter environment. They do not require outer
`UV_NO_SYNC` or `UV_PROJECT_ENVIRONMENT` settings, or a prefilled repository offline cache; run `make bootstrap` first as shown above.
Tests create only their own temporary databases and containers. `make lab` remains a separate learning tool, not a service interface.

For container deployment, read the [Linux operating guide](../ops/linux/README.md) first. The example below runs local synthetic acceptance with replaceable paths;
use a new directory. `--dispose-owned-test-data` explicitly deletes this test project's data.
The source repository must be committed and clean. Secrets enter only dedicated volumes and temporary 0600 files and are not printed to the terminal.

```sh
REPO_ROOT="$(pwd -P)"
RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ledgerops-local.XXXXXX")"
python ops/linux/stack.py --project ledgerops-test-local \
  --state "$RUN_ROOT/stack.json" \
  build --source "$REPO_ROOT" \
  --context "$RUN_ROOT/build" \
  --https-port 18443 --operator-port 19443
python ops/linux/stack.py --project ledgerops-test-local \
  --state "$RUN_ROOT/stack.json" init
python ops/linux/stack.py --project ledgerops-test-local \
  --state "$RUN_ROOT/stack.json" seed-synthetic
python ops/linux/stack.py --project ledgerops-test-local \
  --state "$RUN_ROOT/stack.json" up --operator
python ops/linux/acceptance.py --project ledgerops-test-local \
  --state "$RUN_ROOT/stack.json" \
  --output "$RUN_ROOT/verification.json" \
  --dispose-owned-test-data
```

Acceptance explicitly enables its own synthetic dispatch, then shuts down and cleans up. An ordinary default startup keeps RESTORE, returns ready 503,
disables financial writes and does not start the worker; this is intentional. An ordinary `down` retains volumes when data must be preserved.
Do not clean up this project with `docker system prune`, or apply acceptance fault injection to real data.

## Verification and maintenance for larger workspaces

Financial formats 1–9 remain supported with their original limits of 10,000 rows per table and 16MiB. The new format10 provides a complete,
chunked export through bounded cursors and independent verification using on-disk SQLite. It does not shrink data by truncating or omitting quarantine records.
The CLI requires an explicit choice:

```sh
python -m ledgerops.cli export --format stream10 --workspace WORKSPACE_UUID \
  --output-root PRIVATE_EXPORT_ROOT --code-revision COMMITTED_SOURCE_SHA
python -m ledgerops.cli verify --format stream10 PRIVATE_EXPORT_UUID_DIRECTORY \
  --scratch-root PRIVATE_DISK_SCRATCH
```

Load the protected `LEDGEROPS_EXPORT_DSN` through the runtime environment; do not put passwords in command lines or Git.
[R4 and approval integration](STREAMING-EVIDENCE.md), the [complete source format](STREAM10.md) and
[large-workspace maintenance](MAINTENANCE-CAPACITY.md) describe the limits and operating procedures.
Maintenance keeps the original small-data path and switches larger data to complete on-disk verification under the exclusive barrier.
Maintenance previews explicitly identify their READ COMMITTED capture context; they cannot be presented as read-only RR sources to R4 or R6.

Tests cover a source of about 17MiB, 129 quarantine records of 64KiB each and more than 10,000 request keys.
Declared source limits are 256MiB and one million rows, with 4MiB per row, a 512MiB primary SQLite index and 16MiB of semantic closure. Additional temporary files still require disk space.
Exceeding a limit or leaving semantic checks incomplete fails explicitly rather than returning healthy. These are rejection limits, not throughput or SLO commitments.

Payout capacity queries now use an [active-hold partial index](ACTIVE-HOLD-INDEX.md), reducing the effect of terminal history on admission queries performed while holding locks.
Migration 011 changes only the physical access path; financial row formats and old file semantics remain intact. A regular index build requires a maintenance window.
After restoring an old backup, use the explicit administrator `--index-only` entry point while runtime CONNECT remains closed.
Do not replace that post-restore upgrade with the ordinary migration entry point, which grants CONNECT.

## Recovery, identity and operator actions

- [Authentication and revocation](AUTHENTICATION.md): identity comes from current credential mappings, rechecked inside financial transactions; old-result replay cannot bypass revocation.
- [Controlled reprocessing](OPERATOR-ACTIONS.md): distinct-actor approval bound to current evidence epoch, target versions, restore generation, expiry and the complete incident set.
- [Backup and restore](BACKUP-RESTORE.md): actual pg_dump/pg_restore, same-snapshot business archive, object manifest and invalidation of old credentials/approvals.
- [HTTP interfaces](HTTP-SERVICE.md): business interfaces are separate from the learning lab; ordinary API and operator instances use different capability configurations.
- [Read-only exchange](EVIDENCE-EXCHANGE.md): offline reading with explicit unknown external/COMMIT times and no operation callbacks.

If a backup predates acceptance and there is no independent command archive, the tool reports RESTORE_GAP. It does not infer original request keys
or an original 202 from provider records, and does not pay again automatically. CONNECT, writes and dispatch remain closed after restore; reopening requires the explicit checks in the operating guide.
A business archive in the same database cannot replace an independently durable command archive. Data recovery time is not a measured time to resumed service.

## Scope of deployment claims

The exercised environment is Linux arm64 in local Docker, with actual PostgreSQL and a separate persistent SQLite provider.
Remote deployment, a real provider adapter, a real IdP, host-loss recovery and high availability have not been accepted. Non-root execution, TLS and finite fault tests do not establish those capabilities.
Distinct-actor separation depends on the identity administrator correctly mapping real people; it cannot prevent trusted administrators from colluding.

Workspace exports can check retained original requests but cannot prove the requests were saved correctly in the first place.
Historical permissions and provider HMAC remain explicitly unverified during offline checks without trust keys. A hash provides integrity evidence, not a source signature.
The Work settings disabled browser automation during acceptance. HTTP interfaces, static assets and scripts were checked; visual appearance and real-browser interaction were not accepted.
Implementation, verification scope, actual deployment and personal understanding are recorded separately and cannot substitute for one another.

The current public history has been sanitized, and the original repository is retained as a private archive; see [public history and recorded results](../devlog/docs/PUBLIC-HISTORY.md).
Earlier presentation cleanup is documented in [portability notes](../devlog/evidence/PORTABILITY.md).
Linux acceptance failures retain the stage and bounded command metadata in the report. Raw output remains only in a private diagnostics directory outside the repository;
see the [Linux operating guide](../ops/linux/README.md) for inspection steps.

Demo narration defaults to English. For the original Chinese narration, run
`LEDGEROPS_DEMO_LANG=zh make demo`.
