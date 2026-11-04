# Complete bounded financial stream format10

Format10 exports every allowlisted financial row from one PostgreSQL REPEATABLE
READ READ ONLY snapshot. It retains row-format9 semantics and financial row/constraint
capabilities001–010. The exact checksummed physical-index migration011 is recognized
separately and does not add financial fields; backups retain its actual inventory.
Unknown migrations are still rejected. See [index upgrade](ACTIVE-HOLD-INDEX.md).
Auth credential/ACL tables are excluded as before. Formats1–9 and their
stored bytes remain supported through the legacy interface; format10 is an explicit
choice, not an increased legacy constant.

The complete-directory identity is the manifest export UUID. Readers reject hidden
staging directories even after their manifest has been written. Keep the UUID
basename when copying a completed directory to another parent. A producer failure
before rename removes only its own unpublished staging. A parent fsync failure
after rename can leave a final directory: inspect the reported export UUID; do not
infer that no publication occurred.

The manifest records row-format9, workspace/schema/snapshot metadata, actual code
revision/dirty state, counts for every table including empty tables, ordered chunk
references and exact total rows/bytes. Each canonical UTF-8 JSONL chunk has a table,
zero-based ordinal, exact tuple boundary keys, row/byte counts and SHA-256. Rows are
strictly ordered and unique across chunk boundaries. Chunk names are fixed table
allowlist names, not arbitrary paths. No SQL LIMIT truncates the source. Named
server cursors fetch at most16 rows at a time; SQL preflight rejects oversized
rows before transferring their large fields. Money remains canonical decimal
strings and source row integer fields retain their exact Python integer values.
The selected R6 exchange additionally makes all business sequences/versions strings
for consumers with narrower integer ranges.

The declared finite source limits are 256MiB for manifest plus all chunks, one
million rows including workspace, 1MiB normal chunks, and one oversized row in a
chunk up to4MiB. The manifest itself is capped at4MiB. Any exceeded limit fails
explicitly before publication; there is no silent truncation or claim of unlimited
workspace capacity.

The independent consumer imports no posting helper or database driver. It verifies
all source bytes and schema/identity/count/order bindings, then indexes every row in
scratch SQLite. Global foreign-key and provider-identity checks, journal/account and
immutable-hold folds use the complete indexed source. Requests are validated one at
a time, including more than10,000 keys for one operation. Purchase, reversal,
payout/hold/observation and operator-disposition semantics reuse the existing
independent standard-library checker on complete operation dependency closures.
Maintenance history is checked as a complete closure. A closure over16MiB yields
`verification_complete=false`, `healthy=false` and a named unverified scope; it is
never ignored or represented as an empty healthy history. R4/action adapters reject
that incomplete result.

Reportv4 preserves separate structure/A/B/reservation flags, external consistency
and unresolved counts, with full verification completeness and artifact hashes.
Warnings such as quarantined unauthenticated observations can coexist with a
healthy financial report. Healthy still does not authenticate provider signatures,
prove independent external effects or authenticate historical operator grants.
OPENING funding intent retains its existing unverified boundary. Error details
are capped at1,000 issues with explicit counts/truncation; large values get a
bounded preview and evidence-value digest. A report cannot authorize a write.

SQLite's main index has a512MiB page cap, a2MiB page cache, no memory mapping and
rebuildable scratch journaling. It is removed after success or failure. SQLite can
also create temporary sort files outside the main index; the512MiB index cap is
not a claim to cap all OS temporary storage. Provide at least2GiB of free local
working space for source/staging/index/sort/reference copies, preferably an
operator-controlled volume with a filesystem quota. Source, index and filesystem
limits can fail independently. Disk exhaustion or malformed input fails without a
green report, while original database facts and completed source files remain.

A standalone offline consumer needs just Python3.12's standard library and these
five files in one directory: `stream_verification.py`, `stream_contract.py`,
`verification.py`, `verification_external.py`, and `verification_operator.py`.
No LedgerOps service, PostgreSQL driver, provider process or credentials are needed:

```sh
python -I -S /absolute/consumer/stream_verification.py /absolute/export-uuid
```

The integrated operational CLI exposes explicit stream10 export/verify; the R4
CLI exposes explicit stream10 reconciliation. The R4 compact source has its own
`FINANCIAL_STREAM10_COMPACT_V1` kind. It retains the original manifest and every
chunk under RUN pins and carries the full independent verdict. Only bounded matcher
facts are compacted; opaque quarantine, request, transport and operator history is
still present and checked in the original source. Missing or corrupt opaque chunks
block a typed action. The action loader reconstructs and independently verifies
that full source before comparing the derived target against current locked rows.
The compact matcher is capped at16MiB; existing statement/coverage/namespace limits
still apply. Larger operational histories need further explicitly versioned work;
this release does not silently redefine those contracts.

The concrete old-capacity counterexample used real runtime APIs: two payouts with
holds62+3 accepted129 distinct invalid64KiB messages,65/64 per payout, all below the
100-per-payout quarantine limit. Their hexadecimal evidence alone is16,908,288
bytes, exceeding the old16MiB complete bundle at only129 rows. New format10 exports
all of it and the independent full checker verifies it. Another actual PostgreSQL
case exports and validates10,006 request keys. Actual R4/current publication and
maker/checker execution work on this full source, retaining all source chunk pins.

The isolated same-input memory experiment found that repeated two-character hex
regex groups retained roughly8MiB per maximum-sized match on the tested Python
runtime. Equivalent even-length plus single-character hex checks preserve accepted
bytes and diagnostic order. For the16,979,821-byte source, before/after peak RSS
was565,936,128/48,840,704 bytes, with an identical complete healthy financial report.
These are measured macOS/Python3.12 results for that fixture, not a universal memory
or speed guarantee. The corresponding Linux artifact/resource verification remains
part of the deployment acceptance.
