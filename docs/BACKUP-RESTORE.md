# Local backup and restore contract

Backups retain the complete checksummed inventory of migrations 001–011, including
the physical-index migration 011. The exact historical inventory of migrations
001–010 also remains restorable with runtime CONNECT closed; arbitrary older prefixes
are rejected. After an old restore, the explicit `--index-only` administrator entry
point can add migration 011 without granting CONNECT or changing mode/restore generation.
The general migration entry point grants CONNECT and must not substitute for this
closed upgrade. See [active hold index](ACTIVE-HOLD-INDEX.md).

This procedure uses a trusted administrator connection and real PostgreSQL 18 `pg_dump` / `pg_restore`. It restores only into a new `ledgerops_restore_*` database and a new object directory. It never grants runtime CONNECT, starts traffic or enables dispatch. It is not a real-bank recovery guarantee.

## Artifact and authority

A backup consists of `database.dump`, `business-requests.json`, `manifest.json` and every content-addressed object in the snapshot's registry. The administrator holds one exported REPEATABLE READ, READ ONLY snapshot open while `pg_dump --snapshot` runs. The business archive and object inventory come from that same snapshot. Archive JSON uses UTC timestamps, hex bytea and C ordering independent of the administrator session settings; bounded SQL LIMITs and transaction/tool timeouts fail closed. A later concurrent admission therefore appears in neither the dump nor that archive.

The business archive preserves the original operation/request bytes and key, receipt, payout/provider instruction, hold history and dispatch intent. It is a **same-database archive**, not an independent durable command archive. Its existence does not close a restore-before-admission gap. There is no independently published command archive or publication-before-dispatch acknowledgement in this release.

Files are written into an owned private staging directory, flushed and atomically renamed only after completion. A parent-directory fsync failure after rename reports an error but retains the complete destination for inspection; do not assume failure means no artifact exists or overwrite it. Use inspect and, if durability remains uncertain, create another fresh backup destination. Hashes and lengths detect corruption and missing objects. The manifest is an integrity inventory, not a signature. The dump includes sensitive identity state and database SQL; only restore artifacts from the trusted administrator boundary. Keep backups outside read-only evidence exchange, web roots and source control. Encryption, off-host retention, scheduling, media durability and disaster isolation are separate deployment responsibilities.

The restore environment must provision non-elevated `ledger_migrator` (NOLOGIN), `ledger_app`, `ledger_reader`, `ledger_maintenance`, `ledger_worker`, `ledger_reconciler`, `ledger_auth_reader` (NOLOGIN), `ledger_auth_admin` (NOLOGIN) and `ledger_operator` (NOLOGIN). The `ledger_operator` group inherits exactly the worker/reader/auth-reader groups needed by typed actions; other cross-group or broad built-in data/server privileges are rejected. Deployment login roles may inherit the matching groups. Runtime identities must not inherit `ledger_migrator`. A separately provisioned schema-administrator login may inherit that owner group; owner/superuser administrators are explicitly trusted and remain able to connect while runtime groups are closed. Role passwords and command/HMAC secrets are provisioned outside the backup. The exact installed migration inventory must match the backup; this tool does not silently upgrade an old dump or call migrations, whose setup path grants runtime CONNECT.

## Commands

Install matching PostgreSQL 18 client tools alongside the installed LedgerOps wheel. Supply `LEDGEROPS_BACKUP_ADMIN_DSN` through the administrator's protected environment. It must address the source database for backup, an administrator database such as `postgres` for restore, and the new target database for history recovery. Do not put DSNs or credentials on the command line or in a recorded shell history. `PgTools` also accepts explicit trusted executable argv for isolated validation; the local drills used the repository's pinned PostgreSQL 18.6 Docker image, with secrets forwarded only through environment variables.

Create a backup with a new destination:

```sh
python -m ledgerops.backup_cli backup /protected/backups/new-backup --objects /protected/reconciliation-objects
python -m ledgerops.backup_cli inspect /protected/backups/new-backup
```

Restore into a new database and new object root; IDs must be fresh canonical UUIDs and the database suffix must be 8–40 lowercase letters/digits/underscores:

```sh
python -m ledgerops.backup_cli restore /protected/backups/new-backup \
  --database ledgerops_restore_example_20261029 \
  --objects /protected/restored-objects \
  --restore-id RESTORE_UUID --admin-actor ADMIN_ACTOR_UUID --reason 'local recovery drill'
```

Restore checks every artifact reference before running trusted SQL, then verifies the restored migration checksums, same-snapshot business archive and exact object inventory. Before success, `authentication.invalidate_restored_access` atomically changes every workspace to RESTORE, revokes all restored bearer credentials and changes the audited restore generation. Historical actor/client IDs, approvals and incidents remain; old pending approvals bind the former generation and cannot execute after fresh credential issuance. An open historical freeze and its `current_freeze_id` remain retained, with previous mode/freeze recorded in the restore audit. Handled restore failures after acknowledged database creation drop only the newly created target. A lost CREATE acknowledgement or killed process can leave that closed target for administrator inspection; an interrupted object copy may also leave a private unreferenced directory. Do not infer absence from a failed command or reuse an existing target name.

Creation initially sets ALLOW_CONNECTIONS=false, removes PUBLIC access, then allows only trusted administrator/owner access before restoring SQL; runtime users cannot preconnect in the creation/ACL window. The CLI returns generation/audit references and `runtime_connect_enabled=false`, `external_dispatch_enabled=false`. Bootstrap fresh credentials into the new generation through the existing identity-admin API on the still-closed administrator connection, keeping immutable actor/client identities. Restored tokens are invalid even when their revocation happened after the old backup.

For provider investigation, provision the retained synthetic provider verification keys as an owner-only mode-0600 JSON file. `LEDGEROPS_PROVIDER_TOKEN` is the separate command/history credential. All old verification keys needed by retained events must remain available; missing key material stops recovery.

```sh
python -m ledgerops.backup_cli history \
  --provider-url http://127.0.0.1:PROVIDER_PORT \
  --namespace PROVIDER_NAMESPACE_UUID --keys-file /protected/provider-keys.json \
  --history-file /protected/recovery/new-history.json
```

The new private history file retains exact signed page bytes, namespace and watermark before financial recovery starts. Its returned hash/length can be recorded beside the restore report; `recovery.load_history` replays every signature/coverage check for independent investigation. This is a provider observation artifact, not a missing client-command archive.

This command fetches history only by GET, fixes the first authenticated watermark, then sends that watermark on every subsequent page. It validates the signed page proof, exact payload membership, every event signature and request binding, and contiguous sequence coverage from 1 through the watermark. A later arrival is outside that capture and requires a subsequent capture. Missing, duplicated, reordered or unauthenticated pages/events are errors; a partial capture is never treated as a complete history.

If every external identity has its original local admission, the tool obtains signed LOOKUP results before financial transactions, verifies the retained original request and 202, and feeds HISTORY/LOOKUP into the existing resolver. It does not PUT, POST-close or invent keys. Session workspace barriers serialize cooperating administrators. All runtime identities, including deployment-specific login roles inheriting schema groups, must lack effective CONNECT and there must be no foreign runtime sessions. The established resolver temporarily sees NORMAL only inside this administrator-controlled closed-access procedure; normal completion and handled failures restore RESTORE, preserve any freeze linkage, and release the session barriers. SIGKILL during this window may leave mode NORMAL: runtime CONNECT remains closed and service/worker defaults still refuse financial writes/dispatch. The tool deliberately reports this finite interruption boundary rather than claiming crash-atomic mode transitions.

`RECOVERED` means all retained payouts in this namespace have terminal local resolutions through the captured evidence. `UNRESOLVED` means at least one retained payout still lacks terminal evidence; its hold remains. `BLOCKED` means an existing/arriving contradiction, evidence limit or local obstacle stopped the resolver. `RESTORE_GAP` means authenticated provider history contains an identity absent from the restored admission archive. In particular, a backup before a paid admission lacks the client request key and original 202; provider request bytes cannot reconstruct them. The tool returns the gap, keeps RESTORE and creates no business record. No independent full-archive recovery path is claimed.

There is no activation command in this release. Keep CONNECT, HTTP financial writes and dispatch closed after investigation. Reopening requires a separate operator decision after current independent structure/projection/request-intent checks, latest complete namespace evidence, resolution of every gap/blocker and fresh identity bootstrap. An open pre-existing freeze must go through its explicit reviewed maintenance lifecycle; do not clear its pointer or rewrite its audit to force activation. Never restore an enable flag from artifact metadata.

## Recovery checks and bounds

[Recovery integration tests](../tests/integration/test_backup_recovery.py) use actual PostgreSQL dump/restore, provisioned runtime roles, a persistent provider simulator and a recording HTTP proxy. They cover a provider payment after the snapshot and a missing admission before the snapshot: the first settles from retained evidence, while the second reports RESTORE_GAP without inventing a request key.

The same tests check one shared backup snapshot, GET-only provider recovery, token revocation, immutable objects and open-freeze preservation, corrupt artifacts, unsafe roles, closed service/worker defaults and cleanup after failures. A provider-ahead payout of 60 plus a fee of 2 settles to merchant booked 18 / reserved 0, retaining one journal and the original 202. [Native-tool tests](../tests/unit/test_backup_tools.py) check output limits, password environment handling and command deadlines.

The bounds are a 512 MiB dump, a 64 MiB same-database archive, 250,000 archive rows per selected table/objects, 512 MiB total object bytes, 10,000 history events / 16 MiB total history, at most 32 events per 64 KiB response and 10,000 retained payouts/workspaces. Crossing a bound fails closed. Traffic remains closed after restore; these checks do not establish a service-availability RTO or recovery of unknown commands.

Independent financial verification and provider HMAC verification use separate checks and trust inputs.
