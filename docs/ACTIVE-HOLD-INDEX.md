# Active-hold queries and index upgrades

Payout admission locks the clearing balance before summing the gross amounts of all `HELD` reservations in the workspace, preventing different merchants
from promising the same capacity concurrently. That ordering is unchanged. `UNKNOWN` still has an active hold; an unknown outcome cannot remove its commitment.

Migration `011_active_reservation_index.sql` adds:

```sql
CREATE INDEX reservation_active_workspace_idx
  ON ledger.reservation (workspace_id) INCLUDE (gross_minor)
  WHERE state = 'HELD';
```

The old index contains all historical reservations. The new index contains only records still consuming capacity and covers the columns needed to sum gross by workspace.
Consumed or released reservations remain in the original table and event history; terminal updates remove them only from this partial index.
No history is deleted and no new balance projection is introduced. The query stays inside the original explicit transaction, with unchanged lock order, retained 422 results and replay behavior.

This does not make queries constant-time at arbitrary scale: a workspace with many active holds still requires scanning all of those active records.
The index adds storage and write-maintenance cost. Whether an Index Only Scan can avoid heap access entirely also depends on the visibility map;
the measured sample performed eight heap fetches, so it is not described as always avoiding table reads.

## Upgrade boundaries

The original migration 001–010 bytes remain unchanged. Migration 011 joins the existing checksum and migration history.
It changes only the physical access path, with no new financial columns, constraints, business states or permissions. Complete new databases still use the ordinary migration administrator entry point.

**This is an ordinary transactional CREATE INDEX that may block table writes, not an online lock-free upgrade.** Schedule maintenance for an existing large database:
stop admission, dispatch and other writers, drain in-flight transactions, then execute explicitly. Do not run DDL automatically at business-request startup.
There is no claim of CONCURRENTLY behavior or completion within a fixed time for arbitrary data sizes.

For a database restored from an old 001–010 backup with runtime CONNECT still closed, use the separate narrow entry point:

```sh
python -m ledgerops.db.migrations --index-only
```

Load `LEDGEROPS_MIGRATION_DSN` through a protected environment, never command-line passwords or Git. This entry point requires an idle
autocommit administrator connection able to SET ROLE to `ledger_migrator`; it takes the same transaction advisory lock as ordinary migration.
It accepts only this package's exact 001–010 or 001–011 filenames and checksums. An old state applies and records 011; an already-upgraded state returns an empty list.
Unknown migrations, missing predecessors or checksum differences are rejected. Lock waiting is limited to 5 seconds and statements to 120 seconds, with rollback on timeout.
If a large database needs a longer maintenance budget, assess and change the reviewed implementation first; a timeout is not completion.

The entry point does not GRANT CONNECT or change workspace mode, restore generation, credentials, approvals or financial data.
Restore remains in RESTORE with the original access invalidation intact, followed by the existing independent verification and explicit activation flow.
**Do not substitute ordinary `apply_migrations` for this post-restore step:** the ordinary entry point grants runtime CONNECT.
`restore_backup` does not automatically perform upgrades.

## Export and backup compatibility

Financial export `schema_versions` describes row and constraint capabilities. The financial fields, version semantics and offline verifier for old formats 1–9 and stream10 remain unchanged.
Only when the exact 001–011 list is found and 011 matches the current package's known index-migration checksum does the online exporter separate that physical index marker from the financial capability list.
This cannot conceal an unknown migration; missing 010 or an altered 011 digest is rejected.

Full database backups still retain the actual 001–011 list and checksums, preserving physical migration evidence. The new restorer also accepts exact old 001–010 backups,
keeps CONNECT closed and does not add the index automatically. Other old prefixes or unknown changes remain rejected.
A database can therefore be restored first and upgraded through administrator-controlled `--index-only`, without redefining historical financial files for an index.

## Verification performed

The actual PostgreSQL access-path sample uses 50,000 terminal reservations and eight HELD reservations, each with gross 3, for an expected total of 24.
The default `enable_seqscan=on` remains enabled; the optimizer is not forced to use an index. Before the index, a sequential scan filtered 50,000 rows and accessed 667 shared blocks.
Afterward, the optimizer selected the partial covering index itself, reading eight active records and accessing two shared blocks.
Millisecond timings describe only that sample, not throughput or SLO commitments.

This plan fixture is an isolated physical-access experiment. It uses privileged bypass of business-graph triggers and rolls back everything at transaction end;
**these 50,000 rows are not presented as financially verified business history**. Separate actual admission-concurrency, 422 and terminal-result tests verify financial behavior.
Native dump/restore tests cover new and old backups, a safe index upgrade after old-backup restore, CONNECT remaining closed and unchanged generation/mode.
Original-format exports and complete stream verification continue to pass. Raw plans and test records are in this index change's acceptance directory.
