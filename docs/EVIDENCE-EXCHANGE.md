# Selected synthetic evidence exchange v1

This exchange contains explicit selected payouts and/or journals from one immutable
financial export. It carries opaque account/owner identifiers and provenance. It
has no database credential, network endpoint, callback or write command. The
standalone importer validates schema, hashes, selected references and journal
arithmetic. It does not establish complete-ledger A/B health, historical permission,
provider HMAC authenticity or an implemented Chronicle adapter.

Build from an existing completed export:

```sh
python -m ledgerops.exchange \
  --financial-export /absolute/financial-export-uuid \
  --payout-id 40000000-0000-4000-8000-000000000003 \
  --output-root /absolute/exchanges
```

Selection is required. Repeat `--payout-id` and/or `--journal-id` for up to 1,000
of each. The producer includes journals belonging to selected payouts, explicitly
requested journals, their entries and only the account identities they reference.
Payouts include retained holds, observations, resolutions, original exceptions and
append-only dispositions within the selected scope. It excludes raw provider
requests/envelopes/signatures, credentials, tokens, ACLs and operator identity audits.
Repeated identical source bytes and selection produce identical bytes and dataset
identity; existing publications are validated and retained. New selections or
source bytes produce another immutable directory. Final publication uses same-root
staging, file/directory fsync and rename. If parent fsync fails after rename, a final
directory can exist: inspect or retry the same identity. Hashes detect content
changes; they are not provenance signatures or protection against a trusted owner
replacing both files.

For an optional reconciliation reference, save the JSON stdout from the existing
`ledgerops.recon_cli read` command into a local file, then pass `--pinned-read FILE`.
The producer checks the complete reconstructed reconciliation artifact digest and exact
financial source hash before emitting selected namespace/date/window/watermark,
run/hash, reader-pin and publication-epoch references. This is a historical pin
reference; it does not assert that a released reader pin remains active or that
the run is current when imported. The source reconciliation RUN objects remain under their
original retention policy. No key file is added to the exchange.

Copy `src/ledgerops/exchange_consumer.py` and the two-file output directory to
another machine. Python 3.12's standard library is sufficient, with LedgerOps,
PostgreSQL and provider processes absent:

```sh
python -I -S /absolute/exchange_consumer.py /absolute/exchange-directory
```

The consumer returns a small deterministic summary and exits 0 after validation.
It exits 2 on unsupported major/minor versions, units, unknown/missing fields,
duplicate JSON fields or identities, invalid references, noncanonical bytes,
size/hash mismatch, unbalanced selected journals or unsupported semantics. The
contract is strict v1. Future fields need a new explicit version and reader support;
unknown values are never silently interpreted as ordinary known states.

`manifest.json` names `records.json`, its byte count, SHA-256 and per-table row
counts. The money contract is exactly currency `PLN`, unit `PLN_minor`, scale 2.
Money, source sequences, business versions, epochs and watermarks are decimal
integer strings, including values beyond JavaScript's exact integer range. UUIDs
remain opaque canonical strings. Source export ID, both source-file hashes,
original Git revision/dirty flag, financial schema/format, schema/projection epochs,
workspace mode and PostgreSQL snapshot witness are retained. This metadata states
source claims and snapshot membership; the source export bytes themselves and
code/identity authenticity are not independently authenticated by this exchange.

There is no clock-based inclusion filter: the cutoff is membership in the one
source REPEATABLE READ snapshot. The provider event contract has no execution
timestamp, so observations explicitly use `external_event_at: null` and
`UNKNOWN_PROVIDER_CONTRACT_NO_TIMESTAMP`. No external execution time is inferred
from `received_at`. A journal's `recorded_at` and a resolution's `resolved_at`
remain local recording facts; neither proves database COMMIT time, which is
explicitly unknown. The financial export has no capture timestamp, so its snapshot
time is also unknown. A pre-v6 payout has an unknown source sequence rather than
an invented zero. Reconciliation business dates and fixed source watermarks remain
separate from all these time concepts.

The v1 golden fixture imports without producer modules. Its source is the unchanged
historical financial export v1 and its purchase journal has independent literal
80/80 debit/credit totals. Tests also exercise real PostgreSQL repair/approval/
resolution -> financial snapshot -> selected exchange -> isolated `python -I -S`
consumer, and real pinned reconciliation references. They preserve original exceptions and their
dispositions. They do not claim that Chronicle's own adapter has been implemented.

The selected exchange requires a readable complete source export. Minor version 0 retains
the legacy 10,000-row/16-MiB source limit. Minor version 1 accepts complete format 10
sources through their separate bounded verifier; do not treat a smaller selected
output as a complete-source capacity guarantee.

Exchange minor version 1 supports the explicit `FINANCIAL_STREAM10` source kind:

```sh
python -m ledgerops.exchange --financial-stream /absolute/export-uuid \
  --journal-id 50000000-0000-4000-8000-000000000002 \
  --output-root /absolute/exchanges --scratch-root /absolute/scratch
```

The producer first validates and fully checks the complete chunked source, then
collects only the selected allowlisted records into the bounded exchange. The
manifest retains row format 9, chunk count, original stream-manifest hash and a
content hash explicitly defined as concatenated JSONL bytes in manifest order.
It preserves the full checker summary/completeness and report hash; failed or
incomplete global checks remain visible as `source_healthy=false`. Selected records
do not replace that source verdict. The consumer recognizes this exact minor version 1
provenance structure and rejects unknown extensions. Historical minor version 0 files and
their frozen golden bytes are unchanged. Minor versions 0 and 1 remain distinct source
contracts; stream provenance is never silently inserted into a minor version 0 file.

Minor version 1 also accepts a saved pinned read from the explicit stream-backed reconciliation source.
It checks the reconstructed reconciliation artifact and requires the original stream-manifest
and content hashes to match. The reference keeps `matcher_data_sha256` separately
from `full_stream_manifest_sha256` and the full source content hash; compact matcher
bytes are never mislabeled as the original full history. Reader pin identity,
selected business date/window and both source watermarks remain intact under an
explicit historical stream-pin status. The isolated consumer recognizes this exact
minor version 1 reference shape. Historical minor version 0 reference fields remain unchanged.
