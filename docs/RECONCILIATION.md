# Settlement statements and offline reconciliation

This layer compares local accounting with the provider's own settlement records.
It does not refund payments, release holds or change the ledger. Purchases are
internal transfers without external settlement rows. A payout of principal 60 plus
a fee of 2 should correspond to one provider effect with gross amount 62.

## Obtain provider evidence first

The simulator has its own SQLite file. Settlement assignment supplies existing
payment identifiers and business times; amounts, merchants and beneficiaries come
from its retained requests and effects. A ledger export cannot generate independent
provider evidence.

Batch JSON has four fields: `batch_id`, `effective_at` (UTC seconds),
`fee_rule_version` (`quoted_fee_v1`) and `rows`. Each row contains only `row_id` and
an explicit `provider_keys` array. UUIDs and keys must identify real existing objects.
A batch belongs to one workspace; each row belongs to one merchant/currency. A
payment key can be assigned only once. A group may be split across rows, but every
row must name its members. Run these commands from the repository, injecting secrets
through the environment:

```sh
python -m provider_sim.settlement \
  --database /absolute/path/provider.sqlite3 \
  --namespace "$PROVIDER_NAMESPACE" --key-id "$PROVIDER_KEY_ID" \
  assign --batch-file /absolute/path/batch.json

python -m provider_sim.settlement \
  --database /absolute/path/provider.sqlite3 \
  --namespace "$PROVIDER_NAMESPACE" --key-id "$PROVIDER_KEY_ID" \
  statement --workspace "$WORKSPACE_ID" --date 2026-10-29 --version 1
```

`LEDGEROPS_PROVIDER_HMAC_KEY` must match the signing configuration of that existing
provider file. A wrong path fails explicitly rather than creating an empty database
and signing a statement that says no payments occurred. Save the JSON output as a
statement file. This local administrator CLI is not a public authorization interface;
`provider_sim` is separate from the installed ledgerops wheel.

Versions increase from 1 for each workspace/date/timezone. Requesting an old version
again returns its original bytes. Later settlement assignments create a new version.
Two fixed watermarks describe execution events and settlement assignments separately;
new assignments do not advance the execution-event watermark.

## Capture a local export, then compare offline

Follow the [export instructions](EXPORT-AND-VERIFY.md) to capture one REPEATABLE READ
snapshot. The signature-verification file is a JSON mapping of key IDs to hexadecimal
keys, readable only by its current owner. Keep it outside Git. At most eight trusted
key IDs are supported, and the namespace must be explicit.

```sh
python -m ledgerops.recon_cli match \
  --ledger-export /absolute/path/exports/EXPORT_UUID \
  --statement /absolute/path/statement.json \
  --namespace "$PROVIDER_NAMESPACE" \
  --keys-file /absolute/path/private/provider-keys.json
```

The command needs no database connection. A source checkout records actual HEAD and
dirty state. An installed wheel requires `--code-revision` with the full 40-character
implementation commit, plus `--dirty` for a development build. The export's source
revision and the current matcher's revision are recorded separately; an old export
revision cannot stand in for a new algorithm's identity.

Stdout contains a JSON report. Exit 0 means reconciled within the report's declared
scope. Exit 1 means usable input with differences or pending work. Exit 2 means
unusable input or configuration. Scripts must check the exit code; the existence of
JSON is not a passing result. Missing files, symlinks, FIFOs and oversized input fail
explicitly rather than being truncated into a healthy report.

## Read the report

- `usable` states whether inputs can be interpreted safely. `reconciled` applies to
  this report's explicit scope, not settlement completion across the whole system.
- `issues` follow a stable order. Read `category` and object references first, then
  expected/actual values. Equal amounts with different merchants are still
  WRONG_OWNER. Elapsed time cannot turn an unknown outcome into success or failure.
- `groups` retain the full group key, members, principal, fee and gross. Missing
  membership is AMBIGUOUS_GROUP; the matcher does not guess.
- `matched_provider_keys` and group matches both account for relevant contradictions;
  matching totals alone are insufficient.
- `unsettled_provider_keys` lists keys from the complete provider-effects inventory
  that lack a settlement assignment. Their presence sets `settlement_complete=false`
  and `reconciled=false`. Individual financial fields can match without establishing
  settlement. With no execution time, these keys cannot be forced into a particular
  business day or assigned an invented future settlement time.
- `internal_verification` includes independent structure/A/B checks. Those checks
  and matching an external statement establish different properties.
- `inputs` fixes raw-byte digests and lengths, source versions and watermarks,
  business windows and matcher revision for reproduction and later publication.

Difference categories are MISSING_INTERNAL, UNEXPECTED_INTERNAL, DUPLICATE_EFFECT,
WRONG_OWNER, WRONG_AMOUNT, CURRENCY_MISMATCH, MISSING_PROVIDER, UNEXPECTED_PROVIDER,
TIMING_ONLY, FEE_MISMATCH, AMBIGUOUS_GROUP, INPUT_CONTRADICTION and CORRUPT_INPUT.
These are investigation categories, not automatic verdicts about whose economic
account is necessarily true.

## Time and scope

The complete effects inventory compares amounts and identities within fixed
workspace/namespace inputs. The date window compares settlement groups for that day.
The report separates inventory_scope from settlement_scope; an old difference is
not presented as a new difference today. Business days use the Europe/Warsaw local
calendar to produce half-open UTC windows. A daylight-saving transition day can have
23 or 25 hours.

payout events have no actual execution time, and settlement processing does not invent
one. `effective_at` is the settlement effective time assigned by the provider
administrator; `recorded_at` is when that assignment was retained. HTTP receive time
replaces neither. Local journal.recorded_at is local posting time. TIMING_ONLY
requires fully matching economic fields and explicitly different windows. A provider
payment with only a local HELD state remains MISSING_INTERNAL; possible asynchronous
processing does not hide it.

HMAC establishes agreement with the signing configuration. It does not prove that the
provider never lies, that its database never lost records or that original requests
were captured correctly. Exports from two separate stores are not a distributed atomic
snapshot. Bounds are 10,000 rows per ledger table and 16 MiB per ledger package;
statements allow at most 10,000 effects/members, 1,000 rows and a 16 MiB signed envelope.
Exceeding a bound fails; throughput and capacity depend on actual measurements.

## Publish, read and recover interrupted runs

`match` remains a one-off offline report without a database connection. `publish`
uses migration 008's separate `ledger_reconciler` login and a separate object directory.
The role cannot write balances, entries or holds. Inject credentials through
`LEDGEROPS_RECON_DSN`; do not put a DSN in command arguments or Git.

```sh
python -m ledgerops.recon_cli publish \
  --ledger-export /absolute/path/exports/EXPORT_UUID \
  --statement /absolute/path/statement.json \
  --namespace "$PROVIDER_NAMESPACE" \
  --keys-file /absolute/path/private/provider-keys.json \
  --objects /absolute/path/evidence-objects \
  --run-id "$RUN_UUID" --workers 2 --partitions 16 --timeout-seconds 300
```

Generate and save the run UUID first. It identifies fixed inputs and a plan, separately
from a financial operation ID. Retrying a run requires identical bytes, versions and
plan; different inputs require a new run. `--workers` accepts only 1, 2 or 4. Partition
planning is independent of worker count. Worker processes receive authenticated facts
and a plan, without database connections or publishing authority. The parent verifies
every result before publishing. These are trusted processes under the same operating
system user, not a security sandbox.

Exit 0 from `publish` means its complete publication protocol finished, not that the
report contains no differences. `published=false` means this run is complete but
another run updated current for the same scope first. It does not silently retry to
overwrite that run. Complete objects remain fixed. Current is scoped by workspace,
namespace and date; each successful update increments the workspace's evidence epoch.

Read through a separate `ledger_reader` connection using the same DSN environment
variable. Select a fixed run or explicitly request current within a scope:

```sh
python -m ledgerops.recon_cli read \
  --objects /absolute/path/evidence-objects --workspace "$WORKSPACE_ID" --run-id "$RUN_UUID"

python -m ledgerops.recon_cli read \
  --objects /absolute/path/evidence-objects --workspace "$WORKSPACE_ID" \
  --current --namespace "$PROVIDER_NAMESPACE" --date 2026-10-29
```

The reader first durably pins a run reference, then verifies every referenced file.
`is_current_at_pin` and `evidence_epoch_at_pin` describe only the instant of pinning.
Later publication during the read cannot change pinned contents. `published_epoch`
is the historical epoch when that run was published, not permanent validity.

Read exit codes 0/1 mean passing/differences within the report's scope. Corrupt files,
incomplete runs and incorrect privileges return 2; the reader does not silently
substitute another report. Normal completion or a read failure releases the reader
pin. Stderr prints the reader ID. If a process or connection interruption leaves a
pin, first confirm the reader has stopped, then release it explicitly:

```sh
python -m ledgerops.recon_cli release-pin --workspace "$WORKSPACE_ID" --reader-id "$READER_UUID"
```

A worker exit, timeout or incomplete result terminates the owned worker group and
attempts to mark the run FAILED. A database disconnect can occur after COMMIT, so
`outcome_unknown=true` does not establish rollback. Read status by the returned run ID,
then recover with the original inputs and plan. An error response is not permission
to change identity and overwrite current evidence. SIGINT/SIGTERM cleans up owned
workers. Forcibly killing the parent cannot guarantee cleanup; remaining STAGING
objects are not complete evidence.

Files are written privately, fsynced, atomically renamed under the registry lock,
followed by directory synchronization and reference registration. This is not a
distributed transaction between the database and filesystem: a pre-registration crash
can leave unreferenced files. There is no automatic garbage collection, and object age
is not a deletion rule. Backups must retain the registry, referenced objects and key
references together. Before removing unknown files, stop all publishers and confirm
both durable references and pins are absent. Restore damaged objects from verified
backups rather than overwriting them with a new meaning.

Financial export v7, introduced after migration 008, preserves v6's financial row
shape. Read publication objects through fixed runs as described above. Existing
v1-v6 exports remain verifiable; changing an old file's version number to 7 does not
supply the missing evidence.
