# Local Linux deployment

This stack runs the synthetic service on one trusted host. Images install the built
wheel, the separate provider simulator and bounded operator launchers. Runtime
containers do not mount the source tree or tests. The default exposes TLS only on
127.0.0.1, keeps RESTORE enabled and financial writes disabled, and starts no worker.
The exercised platform is Linux arm64 under Docker Desktop; other architectures
and remote deployments need their own acceptance checks.

Builds pin official Python 3.12.15, PostgreSQL 18.6 and Caddy 2.11.7 image digests.
Build and runtime dependencies use exact versions and download hashes.
source-provenance.json records the Git revision, dirty flag and base images. Image
IDs identify artifacts; they do not replace source review. The build removes the
Caddy binary's low-port file capability and runs with cap_drop: ALL on ports
8443/9443. See the [official image](https://github.com/caddyserver/caddy-docker)
and [release](https://github.com/caddyserver/caddy/releases/tag/v2.11.7).

## Build, initialize and start

Use Docker Engine, Compose and Python 3.12. The exercised versions are Engine
29.4.3 and Compose 5.1.3. Start from committed, clean source and new state/build
paths. The state file contains no database passwords or tokens.

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  build --source "$PWD" --context /protected/ledgerops/build-context
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json init
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json up --operator
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  ca --output /protected/ledgerops/local-ca.crt
curl --cacert /protected/ledgerops/local-ca.crt https://localhost:8443/health/live
curl --cacert /protected/ledgerops/local-ca.crt https://localhost:8443/health/ready
```
Default liveness returns 200 and readiness returns 503. Default up stops a
previously enabled live worker; omitting --operator stops a previously enabled
operator instance. Old processes must not retain earlier activation flags.

init is an explicit one-time administrator job: it initializes owned volumes and
random secrets, starts PostgreSQL as UID 999, then creates NOLOGIN groups, restricted
logins and migrations. Request-service startup does not migrate, create roles or
bootstrap identity. Reinitialization preserves passwords, provider namespace, HMAC
key, SQLite, database and objects. An interrupted partial secret directory is not
automatically overwritten or rekeyed.

Python processes use UID/GID 10001. Capability-specific secret volumes contain
UID-owned mode-0600 files. The API does not mount operator, worker, provider or
administrator secrets. ledger_service has app/reader/auth_reader membership;
ledger_action_operator separately inherits the required operator/worker/reader/
auth_reader groups. Migration credentials are administrator-only; runtime cannot
inherit schema ownership. DSNs, bearer tokens and HMAC keys stay out of Compose
environment, arguments, URLs, browser storage and logs. Private JSON launchers load
allowlisted configuration inside the process.

Only the proxy joins ingress and the private network, using fixed trusted addresses.
Database, API and provider stay on the internal private network; PostgreSQL has no
host port. Port 9443 serves the separate operator instance and also defaults to
loopback. PostgreSQL/SCRAM traffic relies on this trusted single-host network;
end-to-end network TLS and cross-host isolation have not been exercised. Caddy uses
a local CA, which clients explicitly trust. skip_install_trust avoids changing the
container trust store. Do not use -k or bypass browser certificate warnings. See
[the option's meaning](https://caddyserver.com/docs/caddyfile/options#skip-install-trust).

## Disposable synthetic acceptance

Use only a newly created ledgerops-test-* project. seed-synthetic explicitly creates
opening funds of 100 and controlled customer, merchant, maker and checker identities.
Normal initialization does not seed them.

```sh
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json \
  build --source "$PWD" --context /protected/test/context --https-port 18443 --operator-port 19443
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json init
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json seed-synthetic
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json up --operator
python ops/linux/acceptance.py --project ledgerops-test-example --state /protected/test/stack.json \
  --output /protected/test/linux-verification.json --dispose-owned-test-data
```
Acceptance reads secrets into temporary local mode-0600 files and removes them on
exit. Reports retain non-secret metadata. Checks cover UID, file modes, capabilities,
no-new-privileges, read-only root filesystems, resource bounds and absence of source
mounts; CA-verified TLS purchase/replay/content-conflict behavior; hold 62 followed
by actual worker/provider settlement to booked 18, reserved 0, available 18; one
provider effect after restart; independent balance/request checks; live 200 and
ready 503 during database failure; operator reads on read-only object storage;
log redaction, bounded SIGTERM and native backup/closed-target restore.

Capacity acceptance submits 129 invalid observations of 64 KiB through a private
administrator job, then verifies a complete stream source over 16 MiB and a SQLite
scratch index over 32 MiB. A networkless read-only consumer with 4 MiB tmpfs must
return SCRATCH_RESOURCE_LIMIT. Fault injection arrives only through acceptance
stdin; it is neither installed in runtime images nor exposed as an HTTP control.

Startup first observes the actual proxy CA file, then verifies API and operator
/health/live with that CA. Each readiness phase has a 20-second observation budget;
individual Docker probes are bounded at five seconds and HTTP probes at ten, so the
last in-flight probe adds to elapsed time. Waiting follows observed unready state.
Diagnostics record the phase, allowlisted command class/service, return code and
output sizes, excluding arbitrary arguments, Python code, environment and raw
stdout/stderr. Constructor failures retain their phase; cleanup errors cannot replace
the original failure.

A failed subprocess may retain at most 16 diagnostic groups, with the first 8192
bytes per output stream, in a new mode-0700 ledgerops-linux-diagnostics-* directory
under system /tmp. Files are mode 0600. Reports give only directory/file names.
Raw diagnostics can contain credentials: never commit or publish them. Inspect them
locally and remove the private directory when finished. Omitted groups are counted.

Large-source maintenance injects projection drift from 18 to 19, then exercises
freeze, preview, apply and resume back to 18. It verifies the complete stream source,
the report's EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1 context, unchanged hold
and journal history, stable-ID replay and native persistence of resume_report v4.

Worker and provider share only a network namespace. The provider still binds to
127.0.0.1:8765; transport's loopback restriction is unchanged. The worker cannot mount
provider SQLite. Recreating the provider requires recreating the worker, otherwise
the latter retains the old network namespace. This is single-host coupling.

--allow-dirty and --skip-backup-preliminary are development checks, not clean-build
or complete-recovery acceptance. A small example's memory peak is not a capacity,
throughput or SLO promise.

## Resource limits and cleanup

| Component | Process configuration | Memory | CPU | Writable storage |
|---|---|---:|---:|---|
| API | 2 Gunicorn sync workers | 192 MiB | 0.5 | 32 MiB tmpfs |
| Operator | 1 Gunicorn sync worker | 512 MiB | 1 | Dedicated disk scratch + 32 MiB tmpfs |
| Worker / provider | One bounded process each | 192 MiB each | 0.5 each | Worker tmpfs; separate provider SQLite volume |
| PostgreSQL | max_connections 40, shared_buffers 32 MiB | 256 MiB | 1 | Separate database volume |
| Reconciler | Explicit job, one worker by default | 512 MiB | 1 | Separate scratch/object volumes |
| Backup | Explicit administrator job | 768 MiB | 1 | Separate scratch/backup volumes |
| Proxy | Caddy | 96 MiB | 0.25 | CA volume + 16 MiB tmpfs |

Containers also have PID limits, 5 MiB x 2 log rotation and all capabilities dropped.
API liveness does not access the database. Readiness checks the actual schema, roles
and physical restore configuration. Gunicorn gets 45 seconds to drain; container
shutdown is bounded at 50 seconds. These limits are not exhaustive workload tests.

Operator, reconciler, backup and administrator snapshots use dedicated disk volumes
with TMPDIR fixed to /var/lib/ledgerops-scratch. Startup requires at least 2 GiB free.
This checks free space; it does not reserve it or establish a Docker volume quota.
The stream consumer bounds its primary SQLite index at 512 MiB and cache at 2 MiB;
source copies, closure and SQLite temporary files also need space. Run one large
check per job and monitor the Docker data disk. Keep writes/dispatch disabled while
addressing low space; never discard external facts to make room.

Normal exit removes owned ledgerops-stream-* temporary directories. SIGKILL or host
failure may leave them behind. Stop every user of the dedicated scratch volume,
verify no jobs remain, then remove only confirmed temporary directories belonging
to terminated jobs. This does not apply to immutable objects, run pins, provider
history, journals, backups or authoritative records. Disk/permission/parse failures
must preserve unresolved state and UNKNOWN holds.

down retains volumes. dispose-owned-test-data explicitly removes this project's
owned test data after checking every expected volume and the container/network
com.docker.compose.project and io.ledgerops.scope labels. Matching name prefixes
alone are insufficient. Do not substitute docker system prune.

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json down
# Only for a newly created disposable test project:
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json dispose-owned-test-data
```
## Backup, restore and reopening

A separate administrator image uses native PostgreSQL 18.6 tools without Docker.sock.
Only the administrator service mounts its secrets. Backups contain sensitive identity
data and must stay outside public exchange, web directories and Git. Stop admission
and dispatch for the whole namespace before recovery investigation. Never dispatch
from both the original and restored databases.

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup backup /var/lib/ledgerops-backups/new-backup --objects /var/lib/ledgerops-objects
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup inspect /var/lib/ledgerops-backups/new-backup
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup --database postgres restore /var/lib/ledgerops-backups/new-backup \
  --database ledgerops_restore_example_20261029 --objects /var/lib/ledgerops-backups/new-objects \
  --restore-id RESTORE_UUID --admin-actor ADMIN_ACTOR_UUID --reason 'reviewed local recovery drill'
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup --database ledgerops_restore_example_20261029 history \
  --provider-url http://127.0.0.1:8765 --namespace NAMESPACE_UUID \
  --keys-file /run/ledgerops/provider-keys.json \
  --history-file /var/lib/ledgerops-backups/new-signed-history.json
```
The outer backup --database selects the administrator connection database; the inner
restore --database permits only a new ledgerops_restore_* target. Arguments cannot
replace passwords or role identities. Original-command/202 archives share pg_dump's
database snapshot and are not independent command archives. Missing request keys or
original 202 results produce RESTORE_GAP even after provider payment; never invent
business identities. Restore keeps runtime CONNECT closed, workspace RESTORE, old
tokens revoked and old approval generations invalid. It does not reactivate HTTP
writes or dispatch.

Reopening requires an explicit trusted-administrator decision. Configuration flags
do not prove complete recovery. Keep a mode-0600 review referencing backup ID,
restore_id, target database, generation, signed history, watermark, independent
reports, administrator identity and reason. Follow these checks:

1. Verify backup integrity, object inventory and the exact installed migrations.
   Do not invoke ordinary migrations on the restored database: they grant CONNECT.
   For a reviewed physical-index-only upgrade, use the narrow path described in
   [active-hold indexing](../../docs/ACTIVE-HOLD-INDEX.md).
2. With runtime CONNECT still closed and no foreign runtime sessions, capture the
   latest complete signed namespace history and per-payment LOOKUP. Require
   RECOVERED with no gaps, unresolved work, blockers, contradictions, evidence limits
   or missing keys. Recapture after new external events; an old hash is not freshness.
3. Independently export/check structure, projection and request intent, including
   active holds, original ownership/202 and terminal effects. Sources beyond legacy
   row/16 MiB bounds need complete stream verification with verification_complete=true.
   Keep reports; failure or incomplete verification keeps access closed.
4. Match auth_restore_workspace restore_id, current generation and previous mode/
   freeze linkage to the actual workspace; verify old credentials are revoked.
   Explicitly bootstrap reviewed permissions/tokens in the new generation through
   the identity Python API on the closed administrator connection, preserving stable
   actor/client IDs. Do not revive old tokens or copy stale human approvals.
5. Under each workspace's exclusive barrier, recheck the review. Preserve an open
   current_freeze_id, change RESTORE only to FROZEN, then call resume_workspace with
   the same freeze ID after commit so current checks and resumed audit are recorded.
   Never clear the pointer or forge resume. A reviewed previous NORMAL workspace
   without a freeze may explicitly transition RESTORE to NORMAL under the barrier,
   with before/after state in the private review. Previous RESTORE or inconsistent
   records keep access closed.
6. Keep CONNECT closed until mode handling finishes. Explicitly review required
   internal logins, then grant CONNECT for the target database; API groups are
   ledger_app, ledger_reader and ledger_auth_reader, while worker access is separate.
   Schema owners/superusers remain trusted. Point a new private profile only at the
   reviewed target, start with RESTORE=true and writes=false, and verify schema,
   roles and rejection of old tokens. Separately decide RESTORE=false and
   API_WRITES_ENABLED=true, then DISPATCH_ENABLED=true only for the reviewed synthetic
   namespace. Never recover activation flags from backup metadata.

The administrator transition in step 5 uses SQL parameters and the existing barrier.
It does not replace the first four checks:

```python
# admin is the idle autocommit connection to the closed target; values come from the review.
from ledgerops.authentication import _workspace_lock
from ledgerops.db.maintenance import resume_workspace
from ledgerops.backup import _closed_runtime

_closed_runtime(admin)
with admin.transaction():
    with admin.cursor() as cur:
        _workspace_lock(cur, workspace_uuid, shared=False)
        cur.execute(
            "SELECT mode,current_freeze_id FROM ledger.workspace WHERE id=%s FOR UPDATE",
            (workspace_uuid,),
        )
        mode, freeze_uuid = cur.fetchone()
        if mode != "RESTORE":
            raise ValueError("Restored database mode must be RESTORE")
        cur.execute(
            "SELECT a.previous_mode,a.previous_freeze_id,a.generation,s.generation "
            "FROM ledger.auth_restore_workspace a JOIN ledger.auth_workspace_state s "
            "USING(workspace_id) WHERE a.restore_id=%s AND a.workspace_id=%s",
            (restore_uuid, workspace_uuid),
        )
        audit = cur.fetchone()
        if audit is None or audit[2] != audit[3] or audit[1] != freeze_uuid:
            raise ValueError("Restore audit, freeze linkage and generation must agree")
        if freeze_uuid is not None:
            if audit[0] != "FROZEN":
                raise ValueError("Keep closed and investigate a freeze not originating from FROZEN")
            cur.execute("UPDATE ledger.workspace SET mode='FROZEN' WHERE id=%s", (workspace_uuid,))
        else:
            if audit[0] != "NORMAL":
                raise ValueError("Keep a restore without a prior NORMAL state closed")
            cur.execute("UPDATE ledger.workspace SET mode='NORMAL' WHERE id=%s", (workspace_uuid,))
if freeze_uuid is not None:
    resume_workspace(admin, str(workspace_uuid), str(freeze_uuid), admin_actor_id, reviewed_reason)
_closed_runtime(admin)
# Retain before/after state, reports and same-freeze resume in the private review; decide CONNECT separately.
```
Administrator access can bypass the application protocol, so keep the evidence and
human decision separately. This is not a new financial HTTP interface or a restriction
on database owners. After an unknown commit response, keep physical flags disabled
and inspect actual mode, freeze and restore audit before continuing.

## Worker fail-stop and incident response

Any payout InvariantViolation emits worker_invariant_error and exits the whole
worker with code 2, pausing dispatch for every workspace it serves. An unchanged
record can fail again after container restart. Restart loops are not recovery;
do not delete instructions, incidents or holds, or rewrite journals to skip it.
Ordinary capacity shortage emits payout_blocked without this invariant exit.

For an actual deployment, monitor these signals:

- Structured worker_invariant_error logs, including opaque workspace_id/payout_id;
  supplement with worker_database_error, abnormal worker_stopped and restart loops.
  Never forward tokens, DSNs or raw provider data into alerts.
- Workspace-scoped GET /v1/metrics with evidence:read: unresolved_count,
  oldest_unresolved_seconds and blocked_count. Aging unresolved work and absent
  payout_attempt events can indicate stopped dispatch. HTTP liveness proves nothing
  about worker progress.
- blocked_count counts only durable dispatch_intent.state='BLOCKED'; it may exclude
  the invariant that killed the worker. Zero does not override an error log. Alert
  thresholds, recipients and on-call services need actual deployment configuration.

On an alert, disable dispatch and stop the worker, preserving logs, image identity
and a database backup. Use read-only investigation and a consistent export to locate
the record and independently check structure, balances, request intent and holds.
Keep unknown outcomes held and query the original provider key; do not create a new
payout. Supported terminal LOCAL_BLOCKER cases use current evidence and distinct
maker/checker approval. Other inconsistencies need a cause fix, reproduction and
review. Reenable the worker explicitly only after verification and external-fact
review, then observe both the affected record and other workspaces.

Candidate scans interleave workspaces: one payout each before a second. A circular
UUID cursor advances the next batch's starting point even when all candidates were
capacity-blocked. Thus 21 blocked payouts in one workspace do not exclude another
workspace's payout from the same batch; more workspaces rotate through later scans.
Batch size and network calls remain bounded. Capacity skips create no transport
attempt, change no knowledge and send nothing. The cursor is process-local and
resets at restart; this is workspace rotation, not persistent per-payout backoff or
an arbitrary-load latency SLO.

## Upgrade and rollback

Retain backups and current image IDs before disabling dispatch/writes and draining
services. Apply new migrations through an explicit administrator job; never edit
historical migration bytes. New-image readiness checks the actual migration set.
Keep the previous state file and image IDs. A reviewed older image can be restored
without a migration only when it remains compatible with the current schema.
A migration does not imply a safe down-migration. Database rollback restores to a
new database and repeats provider-ahead, missing-history, generation and freeze checks.

Backup encryption, off-host retention, host/disk fault isolation, scheduling, capacity
alerts and real-domain certificates require deployment configuration. This package
does not activate remote endpoints, real banks, cloud services or multi-host HA.
