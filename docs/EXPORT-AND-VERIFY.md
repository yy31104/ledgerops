# Consistent export and independent verification

Consistent export and offline verification read an existing initialized database with
business data. The [maintenance procedure](MAINTENANCE.md) uses a separate maintenance
login. These commands do not create a permanent database or HTTP service. Tests use
their own temporary PostgreSQL instance and clean it up afterward.

## Database privileges

Export uses the separate `ledger_reader` login. It has CONNECT, schema USAGE and
SELECT on explicitly listed financial and maintenance tables. Reading reconciliation evidence also requires
the pin INSERT/DELETE and specific column-locking privileges needed for evidence
reading; these grant no financial write permission. The reader is neither an owner
nor a member of application or migration roles. A READ ONLY transaction is a second
boundary, not a substitute for database privileges. Tests use real reader credentials
in ordinary transactions to prove that writes, DDL, temporary tables and privilege
escalation fail.

For an existing LedgerOps database, an administrator first creates the reader role and sets
its password privately, then runs migrations. For example, in an administrator's
psql session:

```sql
CREATE ROLE ledger_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_reader
```

The migration connection needs existing `ledger_migrator` privileges. The migrator
does not create roles or passwords:

```sh
# Set LEDGEROPS_MIGRATION_DSN locally; keep credentials out of Git and evidence logs.
PYTHONPATH=src uv run --locked python -m ledgerops.db.migrations
```

Migration `002_export_reader.sql` defines export privileges without changing
`001_foundation.sql` or purchase admission. Migration `003_projection_maintenance.sql`
adds projection maintenance and requires a separate maintenance role before application. Test fixtures show the complete
isolated initialization.

## Commands

Run from the repository root. `LEDGEROPS_EXPORT_DSN` contains reader credentials;
`LEDGEROPS_WORKSPACE_ID` identifies the workspace to export:

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli export \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --output-root ./var/exports
```

The result is `{"export_path":".../<export UUID>"}`. Set `LEDGEROPS_EXPORT_DIR` to
that path to verify it. Verification needs neither a database connection nor a DSN:

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli verify "$LEDGEROPS_EXPORT_DIR"
```

Source execution records current HEAD and dirty state. Installed-package execution
requires `--code-revision <full 40-character build commit>` and `--dirty` if the build
contains uncommitted changes. Running inside a repository does not establish that an
installed package matches it. An explicit revision is the caller's declaration, not
automatic proof of package provenance. DSNs stay out of export files; command errors
report error categories only.

Exit 0 means internal structure/A/B pass. Exit 1 means an inconsistency was detected.
Exit 2 means unreadable input, export failure or invalid package format. V1-v4 cannot
fully verify the lifecycle of nonzero reservations; v5 check A also compares reserved
amounts with the sum of active holds.

## What the report establishes

| Field | Meaning |
|---|---|
| `structure_ok` | Accounts, journal headers, debit/credit entries, line counts and relations satisfy their declared structure |
| `projection_ok` (A) | Independently fold all journal debit/credit totals; v5 also folds immutable hold events into reserved amounts, including accounts with no activity |
| `request_intent_ok` (B) | Effects and retained results agree with saved canonical business requests; v5 includes payout admission and discloses the separate external-terminal boundary |
| `reservation_supported` | V1-v4 require zero reserved amounts; v5 supports hold history and checks its validity through structure checks |
| `healthy` | Structure, A and B pass; v6 additionally requires no detected provider-evidence contradiction. This does not establish payment completion or verified external authenticity |
| `rebuild_eligible` | Structure, B and reservation conditions pass; projection drift itself is eligible for repair |
| `issues` | Affected objects, fields, expected values and actual values |
| `unverified_intent_kinds` | Kinds without a canonical business-intent contract, such as the OPENING test fixture |

If a payload names M1 but entries and balances credit M2, A passes and B reports
`WRONG_OWNER`. If the payload was also captured incorrectly as M2, all three can
agree. A hand-maintained original-input manifest in tests detects that defect. Runtime
B needs no hand-maintained manifest and does not independently prove correct capture
of the external request.

OPENING provides explicit test funding; its payload has no canonical funding instruction.
Verification checks its digest, opening structure and result while disclosing that
intent boundary, without inventing a source of funds. Historical refusals are checked
for retained records and absence of financial effects. Today's balance is not used
to reconsider whether yesterday's refusal was justified.

`rebuild_eligible` is not permission to perform a database operation, nor proof that
files still describe the current database. Maintenance rereads, verifies and computes
target balances inside the exclusive barrier. See [maintenance](MAINTENANCE.md).

## Files and snapshot boundaries

One connection reads all tables inside one `REPEATABLE READ READ ONLY` transaction.
An ordinary export may run concurrently with purchases. Its files describe the pinned
snapshot, which may differ from the latest state when publication finishes. Separate
query snapshots per table can assemble a state that never existed. Concurrency tests
and an isolation-level mutation establish that distinction.

Versions 1-9 contain `data.json` and `manifest.json`. UUIDs are canonical strings;
amounts are decimal integer strings; HTTP statuses, versions and row numbers are
JSON integers, while state labels are strings. Payload bytes use lossless hexadecimal encoding. The manifest records
Git revision, dirty state, row counts, byte size and data digest. The digest checks
corruption; it is neither a source inventory nor an authenticity signature. Bounds
are 10,000 rows per table and 16 MiB for a complete package. Exceeding them fails
without truncation.

Publication writes a temporary package beside its destination, flushes/fsyncs and
verifies it, renames it to a new UUID directory, then fsyncs the parent directory.
Existing exports are not overwritten. The loader rejects temporary directories,
symlink members, FIFOs, incomplete packages, extra members, duplicate keys, wrong
sizes/digests, incorrect scope and unknown versions. If parent-directory fsync fails
after rename, the final package may already exist. Inspect files rather than infer
from the error that publication never occurred. This is not a cross-machine
publication protocol.

An interrupted export can leave hidden `.ledgerops-export-*` temporary directories.
The reader rejects them. After confirming that no exporter is running, these
temporary directories may be removed manually.

Version 1 preserves the financial-table contract from migrations 001/002. Version
2 corresponds to 001/002/003 and adds workspace projection_epoch/current_freeze_id,
freeze metadata, repair headers and complete audit rows. New verifiers still accept
retained v1 packages; replacing the schema list must not invalidate old evidence.
Version 3 corresponds to 001-004 and adds resume_report to freeze records. V1/v2
retain their original interpretation. Version 4 corresponds to 001-005 and adds
reversal_attempts and reversal_effects; B verifies both purchases and reversals.
Older v1/v2/v3 packages keep their rules. Unknown database versions fail explicitly
rather than silently omit new tables.

Version 5 corresponds to 001-006 and includes payouts, reservations,
reservation_events, dispatch_intents, transport_attempts and repair.fold_version.
The verifier uses only the standard library to check payout intent from retained
requests and hold provenance from events; production admission/dispatch code does
not generate expected results. This version does not handle external financial
terminal results and discloses `PAYOUT_EXTERNAL_RESULT` in `unverified_intent_kinds`.
Internal A/B alone does not prove original-request authenticity, quote/beneficiary
configuration authenticity or transport-response signing authority. Older formats
retain their original capability boundaries.

Version 6 corresponds to 001-007 and adds retained provider observations, receipts,
quarantine records, original incidents, terminal financial resolutions and linked
incidents. Report v2 independently checks canonical envelopes, digests, original
request binding, terminal observations, holds and financial effects. `external_evidence`
distinguishes pending payouts, provider contradictions and local blockers. Retained
signatures have no accompanying trusted keys, so
`signature_authenticity=NOT_VERIFIED_NO_TRUST_KEYS`. A financial export is not an
independent provider history. Pending work and local blockers appear in the report;
`healthy` still describes consistency within the declared scope.

Version 7 corresponds to 001-008 and retains v6's financial row shape. Reconciliation publication
metadata and raw inputs live in separately pinned immutable objects. Version 8
corresponds to 001-009, retains v7's row shape and supports maintenance auditing in
restore mode. Authentication credentials, token digests and permission tables are
excluded from financial exports.

Version 9 corresponds to 001-010 and adds `operator_actions`, `operator_approvals`,
`operator_executions` and `payout_incident_resolutions`. Original incidents remain.
Only a fully verified `REPROCESS_TERMINAL_LOCAL_BLOCKER` execution chain can remove
the corresponding LOCAL_BLOCKER from unresolved blockers. Checks cover exact canonical
intent/proposal bytes and SHA-256, different maker/checker actor IDs, identical
proposal/approval/execution digests, retained terminal observations, one financial
resolution for the payout, retained execution results and complete disposition of the
proposal's exact incident set. Linked provider contradictions, nonlocal categories,
missing executions and incomplete dispositions remain visible. Later incidents are
reported separately.

V9 produces report v3 with `operator_actions` counts and capability disclosures, and
supports retaining that report in historical resume_report fields. These checks
cover structure and opaque identities. An export lacks authentication history and
cannot establish historical authorization, revocation state or human identity. It
explicitly reports `historical_authorization=NOT_VERIFIED_NO_AUTH_HISTORY`; provider
HMAC authenticity remains unverified. Failed nonfinancial operator attempts stay in
controlled action history, outside the financial exchange package. Older v1-v8
fields, report versions and capabilities are unchanged. Substituting a v9 schema
list cannot supply missing evidence to old files.

## Large inputs: explicit format 10

Migration 011 adds only a physical HELD index. Financial `schema_versions` describes
row/constraint capabilities. The index marker is treated separately only when the
known inventory and 011 checksum match exactly, without changing format 1-9 or
stream10 row semantics. Backups retain the actual migration inventory. Unknown
migrations, missing predecessors and incorrect digests still fail. See
[index upgrade](ACTIVE-HOLD-INDEX.md).

The default export format is unchanged. Beyond the older 10,000-row/table or 16 MiB
limits, use `export --format stream10` and `verify --format stream10`. Format 10 reads
all allowed financial tables completely within one REPEATABLE READ read-only
transaction, using bounded cursors, chunk files and a complete manifest. The independent
verifier indexes all records into on-disk SQLite rather than truncating the source.
Report v4 explicitly states whether all verification completed. Exceeded bounds,
missing chunks, exhausted space or unverifiable semantic closure cannot produce a
healthy result. Older format 1-9 bytes and capabilities remain unchanged. Commands,
capacity boundaries and independent input handling are described in [STREAM10](STREAM10.md).

Large maintenance previews use the distinct
`EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1` capture marker. They are captured
inside an exclusive transaction after writers drain, not as public REPEATABLE READ
evidence. Current reconciliation and offline exchange entry points reject that context.
