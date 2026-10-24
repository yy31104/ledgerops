# Chronicle — data, time, publication and API contracts

Proposed contract version `chronicle.contracts.v1`, 2026-10-23. This is design text and illustrative SQL/pseudocode, not an implemented migration or tested program. All choices remain reviewable. Requirement IDs map to [VERIFICATION.md](VERIFICATION.md).

## 1. Time, units and identity

### C-RQ01: three different temporal meanings

| Dimension | Representation | Meaning |
|---|---|---|
| Observation | `observation_month DATE`, always first day; period `[month, month+1 month)` | Month described by a statistic, not when it became known |
| Source knowledge | Release ID, documented source order, `date / instant / unknown` availability evidence | When the source's information can be supported as available; a vintage date is not an intraday clock |
| Actual local knowledge | Receipt timestamp, validated import occurrence, explicit committed snapshot ID | What this installation actually accepted into a published snapshot |

All local instants use timezone-aware UTC. Original source zone/precision is preserved. C1 supports monthly observations and **date-level source cutoffs**. `source_cutoff.exclusive_end_date=2026-03-01` means releases whose documented source date is earlier than March 1, including February 28; it does not mean an exact UTC instant. C2 may accept a source's evidenced timestamp but does not infer one from a day. An instant cutoff is rejected if selected sources cannot establish the requested intraday precision. Ambiguous “as of March” requires clarification before a context is created.

`latest_corrected` resolves the current dataset pointer exactly once. `source_as_of` requires an explicit archive snapshot (or resolves current once and returns it) and a source cutoff. `system_snapshot` requires an existing snapshot ID, prohibits pretending old source dates are local ingestion dates, and uses only that snapshot's batch membership. Historical source selection must say “reconstructed from the retained source archive,” not “our system knew this then.”

### C-RQ02: stable revision identity and ordering

Canonical natural key: `(source_namespace, series_key, observation_month, source_revision_key)`. A source revision key is an adapter-defined, stable source identity: for the initial direct archives, the publication document and its evidenced revision/version, shared by all rows from that release. Table/row/column locators belong to `revision_occurrence`, not to a separate release per row. It is not an import timestamp. A canonical content hash covers raw lexical value, status, unit/metadata version and source evidence identity; duplicate keys with different content are quarantined. A duplicate with identical content adds provenance occurrence if useful, but cannot change the economic value or order.

Every accepted release needs a comparable `source_order BIGINT` **derived from evidenced source ordering**, with a recorded order rule. For dated archives the adapter may encode ordinal calendar day plus an explicit within-day sequence. Sequence zero is permitted only while the source has one unambiguous release for that day. A conflicting second same-day release without an authoritative sequence is quarantined; UUIDs, download order and hashes are never source chronology. Different source namespaces are not silently combined into one series.

Unknown availability may coexist with a known authoritative revision order. Such rows can appear in a latest-corrected view labelled `availability_unknown`. Strict source-as-of selection excludes them, but must also check whether an unknown-time revision with a higher source order could replace the last evidenced candidate. If so, return `unknown_availability` with no authoritative numeric answer; optionally show the older candidate as explicitly incomplete evidence. Do not confidently return the older value merely because the unknown revision was filtered out. If ordering itself is unknown, the batch cannot become an accepted replacement version.

### Numeric policy `decimal12-half-even-v1`

- Original lexical strings are retained; parse to `NUMERIC(38,12)` only after range/scale validation. Reject NaN, infinities, unexpected separators, excess significant precision and values outside the declared series domain. Do not silently round a source value that exceeds this contract; quarantine it for a schema decision.
- Employment source unit is `thousand_persons`; source integer `157700` stays that unit. Industrial production is an index with explicit base-year metadata and source display precision. Never mix a rounded Board summary with a higher-precision FRED observation under one identity.
- Derived calculations check both intermediate and canonical output ranges; overflow or invalid Decimal quantization returns `NUMERIC_OUT_OF_RANGE` and publishes no partial result. In particular, a small nonzero year-over-year denominator can exceed the output type even when each input fits. Derived calculations use Decimal precision 50 in the Python implementation; a separately written oracle uses rational `Fraction`. Canonical derived outputs are rounded once to 12 decimal places, half-even. Employment differences are exact. Production means may be repeating decimals.
- A difference is the exact subtraction of **stored canonical outputs**, not a newly rounded difference of unavailable unrounded numbers. Decomposition uses scaled integers at 12 decimal places so its additivity can be checked exactly. Display rounding is separate and labelled.
- `missing`, `withdrawn`, `not_available_at_cutoff`, `unknown_availability`, `unit_mismatch` and `incompatible_schema` are distinct statuses. No missing value becomes zero; no three-month mean skips a missing month.

## 2. Proposed relational model and enforcement

The following are critical schema fragments, with routine timestamps/foreign-key names abbreviated only where noted. Full migration authoring must preserve these keys and checks. UUIDs identify entities; SHA-256 strings identify bytes/normalized content, not truth. Objects and report rows are scoped to a dataset/access domain.

```sql
CREATE TABLE dataset (
  dataset_id uuid PRIMARY KEY,
  name text NOT NULL,
  dataset_family_id uuid NOT NULL,
  interpretation_generation integer NOT NULL CHECK (interpretation_generation > 0),
  supersedes_dataset_id uuid REFERENCES dataset,
  owner_scope text NOT NULL,
  current_snapshot_id uuid NULL,
  pointer_version bigint NOT NULL DEFAULT 0,
  CHECK (pointer_version >= 0)
);

CREATE TABLE rights_policy (
  rights_policy_id uuid PRIMARY KEY,
  policy_hash char(64) NOT NULL UNIQUE,
  source_url text NOT NULL,
  checked_on date NOT NULL,
  policy_json jsonb NOT NULL,
  decision_state text NOT NULL CHECK
    (decision_state IN ('synthetic_owned','reviewed_allow','restricted','unknown'))
);

CREATE TABLE storage_object (
  object_sha256 char(64) PRIMARY KEY,
  byte_length bigint NOT NULL CHECK (byte_length >= 0),
  media_type text NOT NULL,
  state text NOT NULL CHECK (state IN ('staged','ready','quarantined','deleting')),
  relative_path text NOT NULL UNIQUE,
  verified_at timestamptz,
  CHECK (object_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE import_batch (
  batch_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES dataset,
  request_identity uuid NOT NULL,
  parser_version text NOT NULL,
  schema_version text NOT NULL,
  state text NOT NULL CHECK
    (state IN ('staging','validated','quarantined','abandoned')),
  received_at timestamptz NOT NULL,
  rights_policy_id uuid NOT NULL REFERENCES rights_policy,
  UNIQUE (dataset_id, request_identity)
);

CREATE TABLE source_payload (
  payload_id uuid PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES import_batch,
  object_sha256 char(64) NOT NULL REFERENCES storage_object,
  source_uri text NOT NULL,
  received_at timestamptz NOT NULL,
  response_metadata jsonb NOT NULL,
  UNIQUE (batch_id, object_sha256, source_uri)
);

CREATE TABLE series (
  series_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES dataset,
  source_namespace text NOT NULL,
  series_key text NOT NULL,
  UNIQUE (dataset_id, source_namespace, series_key)
);

CREATE TABLE series_metadata (
  metadata_id uuid PRIMARY KEY,
  series_id uuid NOT NULL REFERENCES series,
  metadata_hash char(64) NOT NULL,
  unit_code text NOT NULL,
  frequency text NOT NULL CHECK (frequency = 'monthly'),
  scale_exponent integer NOT NULL,
  adjustment text NOT NULL,
  index_base text,
  source_display_decimals integer,
  schema_version text NOT NULL,
  UNIQUE (series_id, metadata_hash)
);

CREATE TABLE source_release (
  release_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES dataset,
  source_namespace text NOT NULL,
  source_revision_key text NOT NULL,
  source_order bigint NOT NULL,
  source_order_rule text NOT NULL,
  availability_precision text NOT NULL CHECK
    (availability_precision IN ('date','instant','unknown')),
  release_day date,
  release_instant timestamptz,
  source_timezone text,
  evidence_object_sha256 char(64) NOT NULL REFERENCES storage_object,
  UNIQUE (dataset_id, source_namespace, source_revision_key),
  UNIQUE (dataset_id, source_namespace, source_order),
  CHECK (
    (availability_precision='date' AND release_day IS NOT NULL AND release_instant IS NULL)
    OR (availability_precision='instant' AND release_day IS NOT NULL
        AND release_instant IS NOT NULL AND source_timezone IS NOT NULL)
    OR (availability_precision='unknown' AND release_day IS NULL AND release_instant IS NULL)
  )
);

CREATE TABLE revision (
  revision_id uuid PRIMARY KEY,
  series_id uuid NOT NULL REFERENCES series,
  observation_month date NOT NULL,
  release_id uuid NOT NULL REFERENCES source_release,
  metadata_id uuid NOT NULL REFERENCES series_metadata,
  kind text NOT NULL CHECK (kind IN ('value','missing','withdrawn')),
  value_numeric numeric(38,12),
  raw_lexical_value text,
  normalized_hash char(64) NOT NULL,
  CHECK (EXTRACT(DAY FROM observation_month) = 1),
  CHECK ((kind='value' AND value_numeric IS NOT NULL)
      OR (kind IN ('missing','withdrawn') AND value_numeric IS NULL)),
  UNIQUE (series_id, observation_month, release_id)
);

CREATE TABLE revision_occurrence (
  occurrence_id uuid PRIMARY KEY,
  batch_id uuid NOT NULL REFERENCES import_batch,
  revision_id uuid NOT NULL REFERENCES revision,
  payload_id uuid NOT NULL REFERENCES source_payload,
  source_locator text NOT NULL,
  UNIQUE (batch_id, revision_id, payload_id, source_locator)
);

CREATE TABLE snapshot (
  snapshot_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES dataset,
  parent_snapshot_id uuid REFERENCES snapshot,
  manifest_sha256 char(64) NOT NULL REFERENCES storage_object,
  state text NOT NULL CHECK (state IN ('published','retired','blocked')),
  publication_sequence bigint NOT NULL,
  publication_recorded_at timestamptz NOT NULL,
  commit_acknowledged_at timestamptz NULL,
  UNIQUE (dataset_id, publication_sequence)
);

CREATE TABLE snapshot_batch (
  snapshot_id uuid NOT NULL REFERENCES snapshot,
  batch_id uuid NOT NULL REFERENCES import_batch,
  PRIMARY KEY (snapshot_id, batch_id)
);

CREATE TABLE analysis_context (
  context_id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES dataset,
  snapshot_id uuid NOT NULL REFERENCES snapshot,
  knowledge_mode text NOT NULL CHECK
    (knowledge_mode IN ('latest_corrected','source_as_of','system_snapshot')),
  cutoff_json jsonb,
  report_version text NOT NULL,
  numeric_policy_version text NOT NULL,
  rights_policy_version text NOT NULL,
  canonical_parameters jsonb NOT NULL,
  context_hash char(64) NOT NULL UNIQUE,
  CHECK ((knowledge_mode='source_as_of') = (cutoff_json IS NOT NULL))
);

CREATE TABLE report_result (
  result_id uuid PRIMARY KEY,
  context_id uuid NOT NULL REFERENCES analysis_context,
  result_manifest_sha256 char(64) NOT NULL REFERENCES storage_object,
  output_hash char(64) NOT NULL,
  implementation_commit text NOT NULL,
  dependency_lock_hash char(64) NOT NULL,
  state text NOT NULL CHECK (state IN ('published','blocked')),
  UNIQUE (context_id, implementation_commit, dependency_lock_hash)
);

CREATE TABLE metric_row (
  result_id uuid NOT NULL REFERENCES report_result,
  metric_key text NOT NULL,
  observation_month date NOT NULL,
  status text NOT NULL,
  value_decimal numeric(38,12),
  unit_code text NOT NULL,
  evidence_json jsonb NOT NULL,
  PRIMARY KEY (result_id, metric_key, observation_month)
);

CREATE INDEX revision_lookup
  ON revision (series_id, observation_month, release_id);
CREATE INDEX occurrence_revision_batch
  ON revision_occurrence (revision_id, batch_id);
CREATE INDEX occurrence_batch_revision
  ON revision_occurrence (batch_id, revision_id);
CREATE INDEX release_order_lookup
  ON source_release (dataset_id, source_namespace, source_order DESC);
CREATE INDEX release_day_lookup
  ON source_release (dataset_id, source_namespace, release_day, source_order DESC);
```

Additional publication/result records:

```sql
CREATE TABLE publication_request (
  dataset_id uuid NOT NULL REFERENCES dataset,
  request_identity uuid NOT NULL,
  canonical_request_hash char(64) NOT NULL,
  actor_id text NOT NULL,
  committed_snapshot_id uuid REFERENCES snapshot,
  PRIMARY KEY (dataset_id, request_identity)
);

CREATE TABLE comparison_result (
  comparison_id uuid PRIMARY KEY,
  comparison_kind text NOT NULL CHECK
    (comparison_kind IN ('context_difference','controlled_2x2')),
  left_result_id uuid NOT NULL REFERENCES report_result,
  right_result_id uuid NOT NULL REFERENCES report_result,
  q00_result_id uuid REFERENCES report_result,
  q10_result_id uuid REFERENCES report_result,
  q01_result_id uuid REFERENCES report_result,
  q11_result_id uuid REFERENCES report_result,
  comparability text NOT NULL CHECK
    (comparability IN ('compatible','context_incompatible','schema_incompatible',
                      'environment_unavailable','rights_blocked','attribution_incomplete')),
  comparison_manifest_sha256 char(64) NOT NULL REFERENCES storage_object,
  canonical_request_hash char(64) NOT NULL,
  owner_scope text NOT NULL,
  UNIQUE (owner_scope, canonical_request_hash)
);
```

A report always computes the full month range fixed in its immutable context; tool/API period subsets only project rows from that canonical result and do not create a different result under the same cache identity. Comparisons follow the same rule. A comparison publisher validates every referenced result/context scope, permitted combination, canonical numeric policy and object closure. A compatible controlled matrix must have all four result IDs; an incompatible response persists a manifest naming the missing/blocked cells instead of inventing results. Publication is immutable and adds permanent object/result references. `DifferenceCell` references existing metric primary keys `(result_id, metric_key, observation_month)`; no unsupported cell-ID registry is necessary. Exposed `evidence_id` is a `revision_occurrence.occurrence_id` with scope and snapshot-membership checks.

Additional required tables: `audit_event` (actor, action, target ID, event time, immutable details hash), `object_reference` (parent entity and object ID), `reader_pin` (context/snapshot, worker instance, acquired/released time), `publication_candidate` (expected parent, proposed batch-set hash, staged/ready/conflict/abandoned state), `dependency_footprint` and `recompute_change` as specified below. These are implementation entities, not a second database or generic workflow framework.

### Supporting record contracts

| Record | Required fields and key/index contract |
|---|---|
| `publication_candidate` | `candidate_id UUID PK`, `dataset_id UUID FK`, `request_identity UUID`, `expected_parent UUID nullable`, `expected_pointer_version BIGINT`, `manifest_sha256 CHAR(64) FK`, `batch_set_hash CHAR(64)`, `state TEXT` in staging/ready/conflict/abandoned/published; unique `(dataset_id,request_identity)`; validated manifest records the complete intended batch set. A rebase with changed request contents uses a new identity. |
| `object_reference` | `parent_kind TEXT enum`, `parent_id UUID`, `object_sha256 CHAR(64) FK`, PK of all three; object-first index for GC. Separate parent-kind tables with real FKs are preferred; if a shared table is used, publisher procedures validate parent existence/type. No direct client writes. |
| `reader_pin` | `pin_id UUID PK`, `snapshot_id UUID FK`, `context_id UUID FK nullable`, `worker_instance_id UUID`, `job_id UUID nullable`, `acquired_at TIMESTAMPTZ`, `released_at TIMESTAMPTZ nullable`; active-pin index on snapshot with released-at NULL. No automatic TTL-based release. |
| `job` | `job_id UUID PK`, scoped actor, `kind TEXT enum`, `context_id UUID FK nullable`, canonical request hash, `state TEXT` queued/running/succeeded/failed/cancelled, `cancel_requested_at TIMESTAMPTZ nullable`, `worker_instance_id UUID nullable`, bounded error, result reference; index queued jobs by creation order. Atomic conditional claim checks queued state. Initial reassignment waits for a supervisor to establish that the prior worker stopped; lease-only reassignment is not supported. |
| `dependency_footprint` | Composite PK `(result_id,metric_key,output_month,input_series_id,input_month)`; `selected_revision_id UUID FK nullable`, `selected_present BOOLEAN`; `(selected_present = (selected_revision_id IS NOT NULL))`; input-series/month index for affected output lookup. An explicit absent input still has a row. |
| `recompute_change` | `(recompute_job_id,series_id,input_month)` PK; old/new selected revision IDs nullable; old/new status and metadata IDs; change classification source_value/withdrawal/availability/unit/interpretation. This is derived evidence, not authority to rewrite source revisions. |
| `audit_event` | `event_id UUID PK`, actor/scope, action enum, target kind/ID, `recorded_at TIMESTAMPTZ`, canonical details JSON/hash; append-only runtime privileges; target/time index. It is an application audit trail, not external tamper-proof notarization. |

Before publishing a worker result, the database validates that its job remains running under that worker instance and cancellation has not won. Failure to re-establish ownership after a lost connection forbids publication. A scheduler that reassigns work while old workers may still execute would require a generation/fencing contract and is intentionally not smuggled into the initial single-worker design.

### Invariants requiring more than column CHECKs

- A revision's series, metadata and release must belong to the same dataset/source namespace. Enforce composite foreign keys or a write-procedure validation; do not rely on API assumptions alone.
- A revision occurrence's payload belongs to its batch; normalized hashes for a natural key are immutable. On conflict, read and compare the persisted canonical tuple inside the import transaction. A mismatch quarantines the candidate and leaves existing snapshots untouched.
- `snapshot_batch` includes only `validated` batches from that dataset. All referenced objects must be `ready`, hash-verified and rights-allowed. Its canonical manifest must describe **exactly** the stored batch set, metadata versions and object closure. The publication procedure performs this cross-row check while holding the dataset pointer lock.
- Every report row's evidence revision is present through an occurrence in the context snapshot and satisfies its cutoff and scope. A publisher validates this outside the model prompt.
- `metric_row` status/value/units are constrained by a status table and publisher validation. A `missing` result cannot have a numeric value; a value row cannot silently omit contributing revisions.
- Runtime roles have no direct UPDATE/DELETE on revisions, metadata, source evidence, context parameters or published result data. A small migration-owner role owns schema changes. A publisher role has only reviewed procedure access. Harden any `SECURITY DEFINER` routine with a fixed trusted `search_path`, qualified names and no PUBLIC EXECUTE. Context creation is server-authenticated and never accepts a caller-supplied owner scope.
- `snapshot.state`, publication acknowledgement and operational accessibility may change through audited procedures; the underlying immutable manifest and batch membership do not. Blocking a snapshot does not rewrite its historical content.

### Parser/schema repair without rewriting accepted evidence

A parser defect can make the same source revision normalize differently. That is not a new source publication and must not overwrite the original natural key. The recommended repair is a **new interpretation generation**: create a new `dataset_id` in the same `dataset_family_id`, increment `interpretation_generation`, point `supersedes_dataset_id` at the previous generation, and re-interpret retained allowed raw objects with the corrected parser. New series/release/revision IDs are scoped to that generation; raw source identity and original receipt evidence are linked, while the new interpretation time is actual current time. A repaired generation gets its own complete snapshots. Existing contexts retain the old generation and its limitations; optionally mark them with an audited defect notice, without changing their content.

The cross-generation report compares source fingerprints plus explicit unit/schema mappings. It labels differences as parser/interpretation changes, not new economic source revisions. No automatic migration of old contexts occurs. A future retract-and-replace batch design is possible but is not included in this contract. Test a deliberately swapped source column, corrected in a new generation, while the original generation still replays its original output and defect notice.

## 3. Historical selection algorithm — C-RQ03

The simple oracle scans all fixture revision records for one requested `(series, month)`, filters by explicit snapshot membership and known-time eligibility, orders by the independently supplied source order, and returns the greatest allowed revision. It is written without production SQL, production normalization helpers or production invalidation code. Frozen fixtures state the intended order and expected answer before either implementation exists.

An illustrative production query for date-level selection follows. `requested` is generated from the explicit requested series/month grid, so missing data cannot drop a request row. Scope and rights checks occur before this query. Columns are abbreviated for readability.

```sql
SELECT q.series_id, q.month, w.revision_id, w.kind, w.value_numeric,
       w.metadata_id, w.release_day
FROM requested q
LEFT JOIN LATERAL (
  SELECT r.revision_id, r.kind, r.value_numeric, r.metadata_id,
         sr.release_day, sr.source_order
  FROM revision r
  JOIN source_release sr ON sr.release_id = r.release_id
  WHERE r.series_id = q.series_id
    AND r.observation_month = q.month
    AND EXISTS (
      SELECT 1
      FROM revision_occurrence ro
      JOIN snapshot_batch sb ON sb.batch_id = ro.batch_id
      WHERE ro.revision_id = r.revision_id
        AND sb.snapshot_id = :pinned_snapshot_id
    )
    AND (
      :knowledge_mode <> 'source_as_of'
      OR (
        sr.availability_precision IN ('date','instant')
        AND sr.release_day < :exclusive_end_date
      )
    )
  ORDER BY sr.source_order DESC
  LIMIT 1
) w ON true
ORDER BY q.series_id, q.month;
```

For an instant cutoff, a separate typed query requires `availability_precision='instant'` and `release_instant < cutoff_instant`; it is not produced by casting dates. A companion membership-restricted query finds the greatest source order with unknown availability for each requested observation. If it exceeds the chosen candidate order (or there is no eligible candidate), the cell is `unknown_availability` with a null authoritative value. The last evidenced candidate may be displayed separately as incomplete evidence, never silently used in a numeric report. Unknown-time exclusions are also counted to distinguish no observation from uncertain historical availability. A winning `withdrawn`/`missing` revision produces a status, not a fallback to an older value.

For `Q` requested observations and average `r` versions per observation, indexed lookups avoid a naive `Q × N` full scan; the actual join/order path may still inspect/sort `r` candidates. Do not claim universal `O(Q log N)` without examining the plan. A safe upper description for this implementation is per-request indexed lookup plus up to `O(r log r)` candidate ordering, with membership checks; batch sweep selection can later use one ordered scan. Benchmark `EXPLAIN (ANALYZE, BUFFERS)` and record actual examined rows. Denormalizing `source_order` into a constrained immutable revision projection is a candidate only if that join becomes a measured bottleneck.

Feast's current opt-in availability predicate and regression tests are a useful reference, including the risk of accidentally removing unmatched rows after a join. Chronicle does not claim that Feast lacks this feature. [C-01](../01-REFERENCE-REGISTER.md#c-01)

## 4. Atomic publication across PostgreSQL and files — C-RQ04

### Filesystem contract

Supported baseline: a local filesystem with documented atomic same-filesystem rename and fsync semantics; one configured root, no user-controlled paths, no symlink traversal. Windows/network mounts require separate validation before claiming equivalent power-loss behavior. Content path is derived by the service from SHA-256, not supplied by a user. A published object is never overwritten in place.

1. Create a staging ticket/batch in PostgreSQL and record its intended scope/rights. Download or generate into a bounded temporary file on the target volume. Compute hash incrementally; normalize and validate without publishing rows.
2. Flush/fsync the temporary file and verify byte count/hash. Acquire a per-object PostgreSQL advisory lock derived from the hash; this lock works even when the metadata row does not yet exist. Register a `staged` object row and an active staging-ticket reference before promoting the file. Registration, promotion and GC use the same object lock; hash-lock collisions merely serialize unrelated objects. If the content path already exists, verify bytes/length and adopt it only if equal.
3. Promote the temporary file with a supported atomic no-replace operation, or a verified same-lock existing-file check on the exclusively service-owned directory, then fsync the containing directory. Do not use unguarded overwrite rename. Mark the registered object `ready` only after durable promotion; release the object lock. A crash leaves a staged/ready object and auditable ticket. Unexpected unregistered hash files are not deleted online: scan/adoption/collection requires quiesced import and publication, preventing a sweeper from racing an unregistered live writer.
4. Insert normalized revisions/occurrences in a transaction and mark the batch `validated` only after all schema, source-order, unit and policy checks. An identical revision already present may gain a new occurrence; the occurrence's batch/payload/receipt remains distinct.
5. Build a candidate manifest using the **explicit union of the expected parent's batch IDs and new validated batch IDs**. Include object closure, parser/schema versions, source metadata and rights-policy IDs. Canonicalize and durably write that manifest with steps 1–3.
6. Acquire dataset row lock; compare current pointer/version with the candidate's expected parent. Acquire referenced object advisory locks in sorted-hash order, then object/batch rows in sorted-ID order; verify states, staging references and manifest equality. Insert snapshot and `snapshot_batch` rows, insert permanent object references, then CAS-update the current pointer/version. Commit together. No network download or lengthy report computation occurs while holding the dataset lock.
7. Return the committed snapshot ID. Record an optional acknowledgement timestamp in a separate audited update. A lost reply is retried by candidate/request identity and returns the already committed ID. It must not create a second, different snapshot with the same request identity.

Publication pseudocode:

```text
candidate = validate_and_prepare_files(expected_parent, accepted_batches)
BEGIN
  lock dataset FOR UPDATE
  if publication_request already has committed_snapshot:
      return its immutable result after COMMIT
  assert dataset.current_snapshot == candidate.expected_parent
  acquire object advisory locks in sorted-hash order
  lock candidate object/batch rows in stable order
  assert batches validated, object states ready, required references live
  assert canonical_database_membership == candidate.manifest_membership
  assert policy allows persistence and this publication scope
  insert snapshot + complete snapshot_batch + object_reference closure
  update dataset current_snapshot, pointer_version
  persist publication_request result
COMMIT
observe commit acknowledgement; return snapshot_id
```

A conflicting publisher receives `409 SNAPSHOT_BASE_CHANGED`. Rebase is a new explicit candidate built against the new parent and revalidated; do not silently merge conflicting source identities or lose the other import. Two snapshots cannot both claim the same pointer version.

### Reader pins, retention and collection

A context resolves its snapshot once. A running job acquires a pin under a transaction checking snapshot accessibility; all query and file reads use this identity. Published reports permanently reference their context, inputs and artifacts while retained. No read silently substitutes the newest object/snapshot when an old file is absent.

Initial policy is **no automatic deletion of published snapshots or their transitive object closure**. GC handles only abandoned candidates/temporary objects after the staging ticket is ended and a grace period passes. Object deletion takes the same per-object advisory lock as registration/promotion, then the object row lock; it checks no live references or active staging ticket, marks `deleting`, then unlinks. It is restartable. An elapsed grace period is insufficient to close a live staging ticket: require finished/abandoned work with the associated process stopped. Lock hierarchy is dataset row if needed, then object advisory locks sorted by hash, then object rows; import/GC never acquire a dataset lock after an object lock. Publisher cannot attach a `deleting` object. Failure before unlink is retried; failure after unlink leaves a tombstone, never a ready object.

Later explicit retirement prevents new contexts but does not remove old report references. Reader pins are not automatically discarded solely because a TTL elapsed: release requires the job to finish or a supervisor to establish that its process is stopped. After an application crash, a quiesced restart can audit and clear abandoned instance pins. This conservative choice trades disk growth/manual cleanup for a tractable correctness boundary. If lease-based collection is introduced later, it requires a new fencing/read-revalidation design and tests; it is not already guaranteed here.

An operator may explicitly purge a retained snapshot/report after listing affected bundles and confirming the loss of replay capability. This is an irreversible operation, outside default automation. An audit tombstone remains. Bytes blocked by changed rights may be retained only where the policy allows; their unavailability is an explicit limit on reproducibility.

### Crash and restore table

| Point of failure | Observable result | Recovery |
|---|---|---|
| Before durable object rename | No published snapshot | Discard incomplete staging file; retry source request only within adapter contract |
| Object durable, database not committed | Orphan/ready staging object; old current snapshot | Reconcile staging tickets; reuse verified bytes or collect abandoned object |
| Normalized batch committed, candidate unpublished | Old snapshot cannot see batch | Resume publication by identity; never infer membership from `MAX(batch_id)` |
| Publish transaction committed, reply lost | Complete new snapshot exists | Retry returns committed snapshot ID |
| Referenced object missing/corrupt after publication | Typed `SNAPSHOT_EVIDENCE_UNAVAILABLE`; no fallback | Block affected snapshots/results, restore verified copy; log cause and scope |
| Database backup older than files | Extra objects are not authoritative | Restore DB; unreferenced files remain orphans until audited |
| Database newer than available files | Referenced closure incomplete | Keep affected snapshots blocked; restore missing objects or report irrecoverable gap |

Hash matches prove byte identity only. A correctly hashed wrong source value remains wrong; independent document review and numeric fixtures address that separate problem.

## 5. Report definitions, incremental invalidation and bundles — C-RQ05/06

Report `macro_revision_report.v1` is a fixed collection of monthly metrics with a requested month range. Formula identifiers and numeric/unit policy are versioned independently of a presentation template.

```text
employment_delta(m) = employment(m) - employment(m-1)
production_mean3(m) = mean(production(m-2), production(m-1), production(m))
production_yoy_pct(m) = 100 * (production(m) / production(m-12) - 1)
```

Incremental reuse is initially allowed only when knowledge mode, cutoff, requested output range, report version, unit and numeric policy are unchanged; only the snapshot/input selections differ. Other changes take the independent full-recompute path. The year-over-year metric is introduced only with C3 and returns missing on zero denominator. Every formula requires the exact calendar months and compatible metadata; `ROWS BETWEEN 2 PRECEDING` over sparse rows is not equivalent to three calendar months.

Store `dependency_footprint(result_id, metric_key, output_month, input_series_id, input_month, selected_revision_id nullable, selected_present boolean)`. **Absence is a dependency too**: a late new observation must invalidate a result previously marked missing. Keep a change set between two context bases with `(series, month, old_selection, new_selection, metadata_change)`; compare selected revision identities after cutoff filtering, not just raw new-row counts.

For the named formulas, invalidate these output months for each changed input month `m`:

| Metric | Affected outputs |
|---|---|
| Employment delta | `m`, `m+1` |
| Production mean3 | `m`, `m+1`, `m+2` |
| Production year-over-year | `m`, `m+12` |

Intersect with the requested output range, but retain input halo dependencies outside that range. Withdrawal and corrections use the same mapping. A changed source revision after the context cutoff does not change that context's selected value and need not trigger a different result. Changed metadata/unit conversion, arbitrary code, calendar policy or schema causes full recomputation unless a reviewed versioned invalidation rule proves a narrower affected domain. Expanding windows or cumulative features invalidate the entire suffix; do not apply a fixed three-month rule to them.

Incremental computation creates a new immutable result: copy unaffected canonical rows/evidence from the old result into the candidate, replace every affected row including missing statuses, then publish atomically. Do not update rows in an old published result. Validate against a full independent computation before treating incremental work as verified. Shared production helpers are not permitted in the oracle's formula/selection/invalidation implementations.

### Reproducibility bundle

Manifest fields include bundle schema; dataset and snapshot IDs; exact batch/revision/evidence IDs; allowed raw-object hashes; time mode/cutoff/precision; formula/query/code commit; dependency lock hash; interpreter/engine versions; parameters; unit/numeric policy; environment/locale/timezone; ordered output hash; rights-policy IDs; missing-data reasons; and source/transform provenance. Include the exact code archive or an immutable accessible commit plus lockfile, subject to licensing. A Git commit hash without retained source/dependencies is not a promise of indefinite execution.

Distinguish:

- **Identity replay:** retained output/evidence bytes match their hashes.
- **Deterministic numerical replay:** the specified supported environment reproduces canonical metric rows. SQL physical plans, wall-clock time and floating implementations may differ and must not be called byte-identical.
- **AI output replay:** the originally generated response/tool trace is retained where allowed. Recalling the same model name does not guarantee identical future prose or model availability.

## 6. Controlled change explanation — C-RQ07

A general side-by-side comparison can show contexts with different cutoffs/modes and must list every changed condition. A **controlled 2×2 attribution** rejects mismatched knowledge mode, cutoff, requested periods, parameters, units or numeric policy; otherwise a third factor is confounded. The server creates four child contexts/runs for the approved data/code combinations without changing either immutable original context. For scalar canonical metric `f`, run `y00=f_old(D_old)`, `y10=f_old(D_new)`, `y01=f_new(D_old)`, `y11=f_new(D_new)` under the same scope, cutoffs, parameters and numeric policy. Define:

```text
data_effect_at_old_code = y10 - y00
code_effect_at_old_data = y01 - y00
interaction = y11 - y10 - y01 + y00
total_change = y11 - y00
assert total_change == data_effect_at_old_code + code_effect_at_old_data + interaction
```

Store all four run IDs. Do not present these terms as a unique economic causal explanation. Alternative paths through the matrix distribute interaction differently. If one run is missing, policy-blocked or schema-incompatible, return `ATTRIBUTION_INCOMPLETE` with the missing cell; no invented zero. If code also changes the numeric policy, either run all four under one explicit compatible policy or report incompatibility rather than mixing it into “data.”

## 7. API contract — C-RQ08

All errors use `{"error":{"code":"...","message":"...","request_id":"...","details":{...}}}`. IDs in examples are illustrative strings, not real persisted IDs. Authentication supplies actor/scope. Request bodies cannot override rights, ownership or a context's snapshot.

| Method/path | Purpose | Important outcomes |
|---|---|---|
| `POST /v1/imports` | Operator requests allowed bounded adapter/fixture import; `Idempotency-Key` scoped by actor+dataset+operation | `202` job ID; same key+canonical body same job; differing body `409`; rights denied `403`; malformed data `422` |
| `POST /v1/snapshots` | Publish a validated candidate with expected parent/version | `201` snapshot; replay same response; base conflict `409`; incomplete object closure `422` |
| `GET /v1/snapshots/{id}` | Metadata, precision and accessibility | `200`; `403` scope; `410` explicit purge; `503` corruption/restore block |
| `POST /v1/analysis-contexts` | Resolve immutable scope/time/report contract | `201`; ambiguous or unsupported precision `422`; rights/scope `403` |
| `POST /v1/reports` | Run named report under context; choose full/incremental only if implemented | `202` job or immutable cached result; bounded request `422` |
| `GET /v1/reports/{id}` | Canonical rows and evidence IDs | `200`; no substitution of latest snapshot |
| `POST /v1/comparisons` | Compatible results or controlled data×code matrix | `202`; incomplete attribution is a typed result state |
| `POST /v1/jobs/{id}/cancel` | Cancellation request | `202` request recorded; final cancelled only after execution stopped |
| `GET /v1/evidence/{id}` | Policy-filtered source excerpt and locator | `200`; scope/rights `403`; missing bytes `503` |
| `GET /v1/bundles/{id}` | Allowed immutable export | `200`; no-export policy `403`; no implied redistribution permission |

Example context request:

```json
{
  "dataset_id": "macro-archive",
  "snapshot_id": "snapshot-S2",
  "knowledge_mode": "source_as_of",
  "source_cutoff": {
    "precision": "date",
    "exclusive_end_date": "2024-03-01"
  },
  "report_version": "macro_revision_report.v1",
  "parameters": {"start_month": "2024-01-01", "end_month": "2024-01-01"}
}
```

Response includes `context_id`, pinned snapshot, canonical request hash, numeric/rights versions and source precision. `end_month` is inclusive for requested report months; internal iteration converts it to the next-month exclusive end. Every numeric row includes `value_decimal` as a string, unit, status, observation month and contributing evidence IDs. No JSON floating-point money/measurement encoding is needed.

Jobs progress `queued -> running -> succeeded | failed | cancelled`; cancellation request is a separate flag until the worker/database acknowledges it. Only `succeeded` owns a published result. Result publication uses the original context ID even if current snapshot changes. Initial single worker uses DB job ownership; multi-worker reassignment/fencing is a separate release decision, not hidden infrastructure required by this design.

AI tools use the same contexts through narrower schemas in [DATA-RIGHTS-AND-AI-EVAL.md](DATA-RIGHTS-AND-AI-EVAL.md). Model-originated attempts to provide raw SQL, change cutoff, change scope or bypass evidence IDs fail at the tool boundary.

## 8. Worked traces and expected observable results

### T-A: initial publication, revision and late local ingestion

This is a **synthetic timeline**, not a claim this application existed on those dates. January value 100 is source-published February 10 and included in simulated snapshot S1 on February 12. Source revises January to 103 on March 15; a simulated importer receives it March 18 and publishes S2. December is 90 throughout.

- S1 membership has original release only; S2 has both accepted batches. Revision rows are append-only; original 100 remains.
- `source_as_of(S2, exclusive_end_date=March 1)` selects 100, January delta 10.
- `source_as_of(S2, exclusive_end_date=March 16)` selects 103, delta 13.
- `system_snapshot(S1)` selects 100, even after S2 exists.
- `latest_corrected` after S2 publication pins S2 and selects 103. A later S3 cannot change that created context.
- A real import of 2024 archives in 2026 creates only 2026 local snapshots. A request for a nonexistent February 2024 local snapshot returns `404`, not a reconstructed source-as-of result labelled local history.

### T-B: real direct-archive revision example

From the BLS February 2, 2024 table, December/January employment values 157347/157700 give January change `353.000000000000` thousand. From March 8, 2024 they are 157304/157533, giving `229.000000000000`. Comparing canonical report rows yields `-124.000000000000` thousand; both contributing months changed, so attributing it only to January's level is wrong.

Board February 15 summary values for November/December/January are 102.7/102.7/102.6; mean3 is `102.666666666667`. March 15 values are 103.0/102.7/102.2; mean3 is `102.633333333333`. Difference of the stored canonical outputs is `-0.033333333334`. A fresh rounded difference of exact unrounded means would differ at the last decimal; the API deliberately compares stored canonical results. Evidence links identify each release/table and source precision. See the rights/data document for primary URLs and limits.

### T-C: ties, unknown availability, withdrawal and units

Two different January values arrive with the same release key and source order: the second candidate is quarantined `CONFLICTING_SOURCE_REVISION`; current pointer is unchanged. Identical content can be reobserved without changing the winner. A source with a known revision rank but missing availability can appear in latest-corrected labelled unknown; strict source-as-of excludes it and surfaces `UNKNOWN_AVAILABILITY` whenever a higher-order unknown-time revision could change the answer, even if an older evidenced value exists.

An April 2 withdrawal of January wins after that source cutoff and yields `withdrawn`, not February's 100 or March's 103. Earlier source-as-of contexts still select their earlier values. Employment deltas for January and February and mean3 outputs whose windows include January become missing in a new result. A later unit change to persons instead of thousands requires a versioned conversion rule; until supplied, the mixed result is `UNIT_MISMATCH`. The original source number is never overwritten to make units look consistent.

### T-D: crash and publication conflict

Publishers P and Q both prepare against S1. P durably writes files and commits S2; its response is lost. Q acquires the pointer lock and gets `SNAPSHOT_BASE_CHANGED`; it cannot overwrite S2 with a branch that omits P's batch. P retries the same publication request identity and obtains S2 without duplicate membership. A reader with context S1 still sees S1. Killing P before commit instead leaves S1 current and only staged/orphan objects.

### T-E: incremental and interaction counterexample

Synthetic production months November=97, December=100, January=100 give January mean 99. January correction to103 gives mean100. A mean3 implementation that recomputes only January misses February/March outputs; C-T14 must catch this. For a controlled code example, old code reports `x`, new code `2*x`, old data `x=10`, new data `x=12`: matrix values 10,12,20,24 yield data2, code10, interaction2, total14. Claiming total=data+code alone is falsified.

## 9. Contract limits requiring review

Initial source order supports the two named archive families and synthetic records; other sources require adapter-specific ordering/rights review. PostgreSQL/fsync guarantees depend on the actual platform and settings. Snapshot ID is authoritative; exact historical wall-clock visibility is not reconstructed from a pre-commit timestamp. API-level permission checks assume trusted application code and database roles are configured as specified. A compromised owner/host can alter files; hashes and audit checks detect some tampering but do not provide external notarization. No source reading here is an executed verification result.
