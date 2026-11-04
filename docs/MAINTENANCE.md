# Freeze, verify, rebuild and resume, including active payout holds

Larger workspaces automatically switch to complete on-disk verification; the original small-data path is unchanged.
Verification runs in a READ COMMITTED transaction after the exclusive barrier has been acquired, avoiding an old snapshot fixed before the lock.
Repairs still recompute current values and verify the result independently in the same transaction. Report v4 and previews identify that capture context.
A failed B check still blocks rebuild, but does not block resume when structure and A pass.
See [large-workspace maintenance and resource limits](MAINTENANCE-CAPACITY.md).

The implemented local flow is: freeze and drain cooperating write transactions → export and inspect differences → recompute and apply
inside an exclusive transaction → export and verify again → explicitly resume. There is no candidate-balance table or interface for importing balances from a preview.
Freezing the database does not stop external payments. In-flight payout simulator calls may finish. While frozen, new admissions, dispatch claims and observation writes are blocked;
external facts must still be investigated after resume.

## Preparation and identities

Create the separate maintenance login before applying the packaged migrations with the administrator's migration connection:

```sql
CREATE ROLE ledger_maintenance LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_maintenance
```

Migration 004 adds resume-report fields without changing the already-applied 003.

The role has no app/migrator membership, historical financial write privileges or DDL capability. Migration 006 adds reserved projection-write privileges for active-hold rebuilds;
it does not permit changes to holds or their events. The role can change mode, balance projections and maintenance metadata, so correct operation still requires the Python flow.
Arbitrary SQL executed with maintenance credentials does not automatically obey the barrier or verification rules.

```sh
# Set LEDGEROPS_MIGRATION_DSN privately; keep passwords out of Git and evidence logs.
PYTHONPATH=src uv run --locked python -m ledgerops.db.migrations
```

The migrator does not create roles or passwords. From migration 006 onward, a new database requires migrator, app, reader, maintenance and worker roles beforehand.
Worker creation and boundaries are covered by [payouts](PAYOUTS.md). Initialize the workspace and business data before running maintenance commands.

Maintenance commands use `LEDGEROPS_MAINTENANCE_DSN`; ordinary exports still use the reader's `LEDGEROPS_EXPORT_DSN`.
The actor UUID comes from a trusted calling boundary and is an audit identity, not a replacement for authentication.

Fix and save these values before the first operation, then reuse them on retries: workspace `LEDGEROPS_WORKSPACE_ID`,
freeze `LEDGEROPS_FREEZE_ID`, repair `LEDGEROPS_REPAIR_ID` and operator `LEDGEROPS_ACTOR_ID`.
Each must be a canonical UUID. Reason text is retained exactly, without trimming or normalization.

## Run in order

Run from the repository root. Freeze waits for in-flight purchases holding the workspace's shared barrier, then commits FROZEN:

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli freeze \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "investigate balance drift"
```

Subsequent purchases, including replays that need a new request association, return 503 without retaining a new business result.
A failed freeze does not fabricate success. After a lock timeout or cancellation, confirm the outcome using the original identity.
RESTORE is not an ordinary freeze.

Inspect the preview; stdout JSON may be redirected to your own `var/preview-report.json`:

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli preview-rebuild \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --output-root ./var/exports
```

The preview returns `export_path`, `report` and `changes`. Changes contain before/after values for every account,
including zero-activity accounts and unchanged amounts; version fields show what the next repair would write.
Save the export directory's final UUID as `LEDGEROPS_PREVIEW_EXPORT_ID`. It is a later audit reference, not executable balance data.

From v5 onward, balance drift can be repaired when structure, including a valid hold lifecycle, and B pass. Debit/credit totals come from the journal;
reserved comes from active holds tied to immutable HELD events. Rebuild cannot conceal a failed B check, and missing or invalid hold source records also block application.
Old v1–v4 exports lack hold history and retain the original restriction against rebuilding with nonzero reservations.

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli apply-rebuild \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --repair-id "$LEDGEROPS_REPAIR_ID" --preview-export-id "$LEDGEROPS_PREVIEW_EXPORT_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "rebuild balances from journal entries"
```

Apply reacquires the exclusive barrier, checks the current freeze identity, rereads and verifies the ledger, then independently aggregates all accounts in SQL.
Balance updates, complete before/after audits, account versions and projection epoch commit or roll back together in one transaction.
Apply accepts no target balances and does not read values calculated by an old preview. The caller supplies the preview ID;
the tool does not authenticate that it identifies a report the operator actually read. Success leaves the workspace FROZEN.

Existing account versions increase by one; a missing balance row starts at version one; epoch increases by one. Booked remains a database-generated column.
Source entries, business results, holds and events do not change; reserved is a repairable projection. The repair log includes every account and before/after reserved values,
and audit rows cannot be added, changed or deleted after commit. Historical fold_version=1 still requires zero reservations; new repairs use fold_version=2.

Export again and verify offline using the new returned path; see [export and verification](EXPORT-AND-VERIFY.md):

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli export \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --output-root ./var/exports
PYTHONPATH=src uv run --locked python -m ledgerops.cli verify "$LEDGEROPS_POST_EXPORT_DIR"
```

After confirming the result, explicitly resume. Resume verifies current data again inside the exclusive transaction rather than trusting an old report:

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli resume \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "post-rebuild verification passed"
```

A healthy ledger may be frozen, checked and resumed without an unnecessary repair. Resume requires only valid structure and a passing balance check A.
When B fails, the complete report is still saved in immutable `resume_report`; resuming does not turn it into a pass. Valid nonzero holds do not prevent resume.
In v5, reserved differing from authoritative holds fails A and requires rebuild first. Applying a rebuild requires passing structure and B, and permits valid active holds.
The service can establish that current checks passed, not that the operator read the preview or saved another copy of the export report.

## Failures and retries

- The same repair ID and fields return the original repair metadata without incrementing balance versions or epoch again. Different fields return `IDENTITY_CONFLICT`.
  A disconnect during COMMIT does not establish failure; reconnect and reuse the same ID.
- Replaying a closed freeze/resume identity returns historical metadata only and does not act on a newer freeze session.
  Reading a historical success does not establish that the workspace is currently NORMAL; query a fresh snapshot for current state.
- New operations require the current freeze ID. Identities from another workspace or an earlier session cannot affect the current session.
- Failed business preconditions produce CLI exit code 2 and a structured error, such as `INVALID_SOURCE`, `RESERVED_UNSUPPORTED`, `FREEZE_CONFLICT` or `PROJECTION_DRIFT`.
  A successful preview command may exit 0 while reporting drift; inspect the report fields to determine whether apply is allowed. Verify exits 1 when it finds inconsistency.
- SQL, constraint, lock-timeout, cancellation and connection errors are not retried internally. Database transactions roll back atomically;
  a connection error may leave the outcome unknown, resolved through stable IDs. Finite fault tests do not exhaust every possible failure.

## Old-bundle compatibility and scope

New-database export format v5 (migrations 001–006) adds payouts, holds, intents, attempts and audit fold_version. The v4 reversal and v3 resume-report contracts remain supported.
Old v1 (001/002), v2 (001–003), v3 (001–004) and v4 (001–005) data continue to load and verify under their original rules.
The frozen snapshot export bundle has a separate regression test. Unknown newer versions are rejected.
Source commands automatically record the Git revision and dirty state. Installed-package previews require explicit `--code-revision`, just like exports.
File publication and protection retain the snapshot export protocol rather than adding another one.

[Full reversal](REVERSAL.md) is implemented. Complete history after reversal also participates in current verification and rebuild.
Synthetic test opening balances and reservations are not public business interfaces.

Migration 006 explicitly rejects an old database with nonzero reserved but no authoritative hold history. Investigate the source of those values before upgrading;
the migration cannot silently zero them or fabricate history.

From 007 onward, cross-workspace provider conflicts are global diagnostics: a conflict received in another workspace may arise while this workspace is frozen.
The exclusive barrier fixes this workspace's journal/hold financial sources, not every provider's evidence. Related maintenance diagnostics reflect the read performed;
`source_witness` does not establish a shared snapshot for these cross-workspace diagnostics. Ordinary read-only exports still obtain a consistent snapshot in a separate REPEATABLE READ transaction.
Related diagnostics are not inputs to rebuilt amounts.
