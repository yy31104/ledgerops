# Large-source maintenance

Freeze, preview, repair and resume use the same Python/CLI signatures and permission boundary for small and large sources. Small sources use the legacy snapshot verifier and report rendering. When a legacy row/byte limit is exceeded, maintenance observes every current row through a private bounded SQLite assessment.

The private assessment runs inside the maintenance connection's existing READ COMMITTED write transaction, after the exclusive workspace advisory barrier. It checks that context; it never starts another PostgreSQL transaction or switches to REPEATABLE READ after waiting for a lock. Named server cursors preserve complete table coverage, including the transaction's own uncommitted repair/audit writes during its independent post-check. SQL targets still come freshly from journal entries and active-hold history, independently of the verifier's fold. A failed post-check rolls back balances, repair rows, repair declaration and epoch together.

Large preview publication happens after the database transaction using captured rows, caller-supplied source revision and the existing atomic UUID/file protocol. Its format 10 source manifest and returned version 4 report both carry:

```text
capture_context = EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1
```

This is a barrier observation, not a read-only RR export. Standalone stream verification retains the tag. Reconciliation matching, action evidence and offline exchange reject this capture context. Use a fresh ordinary RR stream export for those consumers; a later RR export may contain the saved maintenance review without inheriting its outer capture context.

Repair requires complete structure, request-intent (B) and hold-source checks, followed by structure, projection (A), B and hold checks on the newly written state. Resume requires complete structure and projection checks; B, reservation and external warnings remain visible in the saved review. No hold is released and no provider command is sent by maintenance.

A large resume saves the full bounded version 4 review: flags, issue counts/truncation, explicit unverified scopes, external-authenticity limits, resource limits and the capture context. Its private SQL observation has `source_export_id=null` and `source_manifest_sha256=null`, with a real canonical source-content hash. It does not invent an export ID or Git identity for an unpublished internal observation. The historical-report verifier accepts only the known context/engine/limits and consistent complete flags; unknown/future capabilities cannot fall back to a legacy report.

The assessment preserves the stream limits: 1 million source rows, 256 MiB of canonical source bytes, 4 MiB per row, a 512 MiB SQLite index with a 2 MiB cache, and 16 MiB semantic closures. SQLite bodies use ASCII JSON so the existing `length(body)` closure budget counts bytes accurately for non-ASCII input. Crossing a source, scratch, semantic-closure or existing 10,000-account repair-declaration limit fails without truncation. An incomplete verification cannot authorize repair or resume.

[Maintenance integration tests](../tests/integration/test_maintenance_capacity.py) use PostgreSQL 18.6 and cover 129 unique 64 KiB diagnostics split 65/64 across two payouts, plus more than 10,000 request keys. They check repair back to 80 booked / 65 reserved with immutable holds and history retained; fresh verification after a lock wait; WRONG_OWNER blocking repair while A-only resume keeps its B warning; rollback after a bad post-check; incomplete reviews; barrier/isolation rejection; and multibyte byte-budget accounting. These finite cases do not establish arbitrary-volume throughput or real-provider finality.
