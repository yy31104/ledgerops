# Streaming financial evidence and reconciliation

Format10 removes the legacy per-table10,000-row and16MiB transfer ceiling for the current row-format9 schema (001–010). Formats1–9 keep their existing bytes, readers and limits. Select the new format explicitly; an unknown source kind/version is an error and never falls back to legacy interpretation.

## Complete source and limits

The producer holds one REPEATABLE READ, READ ONLY transaction for workspace metadata and every table in the existing financial allowlist. Named server cursors fetch16rows at a time in the declared tuple-key order. It does not use SQL LIMIT to truncate tables. A conservative server-side row-size check runs before transferring bytea/JSON rows, then exact canonical serialized size is checked again.

A completed UUID directory contains `manifest.json` and canonical UTF-8 JSONL chunk files. Every table has an exact row count, including zero; every chunk binds table/ordinal, filename, row count, byte length, SHA256 and first/last tuple keys. Chunk bytes include each row's final LF. The manifest binds source workspace/schema/snapshot/server metadata, actual source revision/dirty status, total rows and total file bytes. Only the final UUID directory matching the manifest export ID is accepted; an unpublished staging directory is not a completed artifact.

Limits fail the operation instead of dropping rows:1MiB chunks, at most one row in an oversized chunk up to4MiB,1,000,000 total rows including workspace metadata,256MiB total chunk+manifest bytes and4MiB manifest. Handled publication failures remove only the invocation's owned unpublished staging directory. After rename, a failed parent fsync acknowledgement may leave the complete final UUID directory; inspect that identity rather than deleting it or assuming rollback. SIGKILL may leave an unpublished directory requiring explicit owned cleanup.

The independent consumer uses a512MiB scratch SQLite index with2MiB cache. It validates every source byte/row and performs global projection/hold/FK/conflict checks plus complete per-operation semantic closures bounded at16MiB. Requests are checked sequentially, so many keys for one business operation do not become a legacy10,000-row gate. A closure outside the declared budget gives `verification_complete=false`, `healthy=false` and explicit unverified scopes. A successful transfer alone is never a successful financial verification.

Use a private disk scratch directory with sufficient available space for source copies, the index and SQLite temporary work. These finite limits and measured local probes are not maximum-volume capacity or service-latency guarantees. Financial exports exclude credential tables through the established allowlist but still contain business/evidence identifiers and retained payloads; use the selected offline exchange exchange adapter when a smaller read-only exchange is intended.

## Commands

Set protected `LEDGEROPS_EXPORT_DSN` to the dedicated reader login. Installed artifacts require the exact full source Git revision; use `--dirty` only when accurately describing an uncommitted source state.

```sh
python -m ledgerops.cli export --format stream10 --workspace WORKSPACE_UUID \
  --output-root /protected/financial-exports --code-revision COMMITTED_SOURCE_SHA
python -m ledgerops.cli verify --format stream10 /protected/financial-exports/EXPORT_UUID \
  --scratch-root /protected/scratch
```

For existing reconciliation matching, select stream10 explicitly. The statement and retained HMAC verification keys follow the existing protected-file and namespace contract.

```sh
python -m ledgerops.recon_cli match --source-format stream10 \
  --ledger-export /protected/financial-exports/EXPORT_UUID \
  --statement /protected/provider-statement.json --namespace NAMESPACE_UUID \
  --keys-file /protected/statement-keys.json --code-revision COMMITTED_SOURCE_SHA
```

For publication, set protected `LEDGEROPS_RECON_DSN` to the dedicated reconciler login and use a stable run UUID:

```sh
python -m ledgerops.recon_cli publish --source-format stream10 \
  --ledger-export /protected/financial-exports/EXPORT_UUID \
  --statement /protected/provider-statement.json --namespace NAMESPACE_UUID \
  --keys-file /protected/statement-keys.json --code-revision COMMITTED_SOURCE_SHA \
  --objects /protected/reconciliation-objects --workers 1 --partitions 16 --run-id RUN_UUID
```

Stream matching currently supports one worker. It rejects workers2/4 explicitly; the earlier legacy1/2/4 measurements are not a stream benchmark. Legacy CLI behavior remains the default. `match` returns1 for a usable report with differences and2 for invalid/unsupported input. `publish` returns0 when immutable publication completes, independently of reconciliation differences. Read the completed report using the existing `read` command with a dedicated reader DSN before deciding what the report means.

## Full-source authority reaches approvals

`FINANCIAL_STREAM10_COMPACT_V1` is an explicitly labeled bounded view, not a partial legacy export. It retains complete matcher tables (accounts, operations, journals, entries, payouts and payout resolutions), full source manifest/chunk references and the full independent report. Its separate complete target projection contains current account/balance/payout/hold facts, valid observation identity/hash/state metadata and all incident relations required for typed actions. Opaque requests, transport/quarantine bodies and operator histories remain in the original full stream and independent audit; they are not silently treated as empty tables.

reconciliation input manifest version2 pins the original source manifest, every original chunk, compact bytes and statement. Chunk identities also appear in the global partition coverage. Historical version1 manifests remain supported. A missing/corrupt original chunk, including an opaque quarantine chunk unused by the matcher, blocks action evidence reading without selecting an older run.

The action reader verifies pinned objects one at a time, reconstructs a private UUID source directory, reruns the full independent verifier and deterministically rebuilds the compact view. It requires exact compact/report agreement, complete verification and structure/projection/request-intent checks before target/current freshness checks. Legacy evidence retains its64MiB aggregate read guard. Stream evidence has a512MiB aggregate reference ceiling and does not retain all source chunks in an in-memory object dictionary.

reconciliation may publish a complete diagnostic source with failed internal checks so specific differences remain visible. That report cannot authorize a typed action. An incomplete independent verification cannot become compact matcher authority at all. Existing maker/checker distinction, current grants, generation/expiry, evidence epoch, exact observation/incident fingerprints, target versions and single financial effect continue to apply.

Reusing the same run ID and unchanged source recovers a lost response: completed replay retains its original completion, RUN pins and evidence epoch, and consumes redundant PUBLISHER pins. Changed bytes or plans conflict. Actual PostgreSQL proxy tests withhold COMMIT before forwarding and after observing backend COMMIT; both caller outcomes are reported unknown when the failed connection prevents audit completion. Query/retry the same stable run ID on a fresh connection instead of inventing failure or a new identity.

## Local evidence

Actual PostgreSQL18.6 probes cover: exact legacy row equality; more than10,000 request keys;129 unique65,536-byte invalid observations through the public resolver split65/64 across two admitted payouts under each100-row quarantine limit; RR consistency while a purchase commits between exported tables; chunk/row/disk/source-size failures and owned cleanup; one oversized audited row; uncertain final-parent fsync; completed-UUID identity; real reconciliation publication and maker/checker settlement with another active hold retained; complete request-history B rejection; missing opaque source rejection; forged compact rejection; actual CLI routes; stable run replay and two observed COMMIT ambiguity boundaries. Existing reconciliation publication cases also pass through the new reference reader.

The measured constant-space independent consumer handled the public-payload source above the16MiB legacy cap at48,873,472bytes peak RSS in an isolated standard-library process. This observation concerns that specific fixture and implementation. It does not authenticate real-bank finality, establish HA or prove arbitrary workloads fit the configured resource/time limits.
