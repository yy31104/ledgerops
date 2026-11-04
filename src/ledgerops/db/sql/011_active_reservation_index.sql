-- Physical access path only: financial rows, constraints and grants are unchanged.
-- Run via the migration administrator while writes are drained; CREATE INDEX
-- takes a table lock. This transaction-managed migration is not CONCURRENTLY.
CREATE INDEX reservation_active_workspace_idx
  ON ledger.reservation (workspace_id) INCLUDE (gross_minor)
  WHERE state = 'HELD';
