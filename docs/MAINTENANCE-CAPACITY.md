# Large-source maintenance

Freeze, preview, repair and resume keep their existing Python/CLI signatures and permission boundary. Small sources continue through the existing legacy snapshot/verifier and keep their report rendering. When a legacy row/byte limit is exceeded, maintenance observes every current row through the new private bounded SQLite assessment.

The private assessment runs inside the maintenance connection's existing READ COMMITTED write transaction, after the exclusive workspace advisory barrier. It checks that context; it never starts another PostgreSQL transaction or switches to REPEATABLE READ after waiting for a lock. Named server cursors preserve complete table coverage, including the transaction's own uncommitted repair/audit writes during its independent post-check. SQL targets still come freshly from journal entries and active-hold history, independently of the verifier's fold. A failed post-check rolls back balances, repair rows, repair declaration and epoch together.

Large preview publication happens after the database transaction using captured rows, caller-supplied source revision and the existing atomic UUID/file protocol. Its format10 source manifest and returned version4 report both carry:

```text
capture_context = EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1
```

This is a barrier observation, not a read-only RR export. Standalone stream verification retains the tag. reconciliation matching/action evidence and offline exchange stream exchange reject this source capability for now. Use a fresh ordinary RR stream export when those consumers need new evidence; a later RR export may contain the historical saved maintenance review without inheriting its outer capture context.

Repair requires complete structure, request-intent(B) and hold-source checks, then complete structure/projection(A)/B/hold checks on the newly written state. Resume keeps the adopted structure/A policy: complete structure and projection checks are required, while B, reservation and external warnings remain visible in the saved review. No hold is released and no provider command is sent by maintenance.

A large resume saves the full bounded version4 review: flags, issue counts/truncation, explicit unverified scopes, external-authenticity limits, resource limits and the capture context. Its private SQL observation has `source_export_id=null` and `source_manifest_sha256=null`, with a real canonical source-content hash. It does not invent an export ID or Git identity for an unpublished internal observation. The historical-report verifier accepts only the known context/engine/limits and consistent complete flags; unknown/future capabilities cannot fall back to a legacy report.

The assessment preserves the stream limits (1million source rows,256MiB canonical source bytes,4MiB per row,512MiB SQLite index with2MiB cache and16MiB semantic closures). SQLite bodies use ASCII JSON so the existing `length(body)` closure budget counts bytes accurately for non-ASCII input. Crossing a source, scratch, semantic-closure or existing10,000-account repair-declaration limit fails without truncation. An incomplete verification cannot authorize repair or resume.

Local PostgreSQL18.6 tests cover129 unique64KiB diagnostics split65/64 across two payouts plus more than10,000 request keys; drifted projection/reservation repair back to80 booked/65 reserved with immutable holds/history retained; an observed lock wait followed by fresh post-commit verification; WRONG_OWNER blocking repair while A-only resume keeps its B warning; atomic rollback after a bad post-check; incomplete-review refusal; missing barrier/wrong isolation rejection; CJK byte-budget accounting; and existing maintenance/hold/reversal/resume/CLI regressions. These finite cases do not establish arbitrary-volume throughput or real-provider finality.
