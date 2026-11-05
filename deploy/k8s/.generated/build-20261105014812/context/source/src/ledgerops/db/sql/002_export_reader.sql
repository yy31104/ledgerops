-- R2-PR01. The caller provisions ledger_reader as a separate non-owner login.
GRANT USAGE ON SCHEMA ledger TO ledger_reader;
GRANT SELECT ON ledger.workspace, ledger.account, ledger.account_balance,
  ledger.operation, ledger.request_result, ledger.journal, ledger.journal_entry,
  ledger.schema_migration TO ledger_reader;
