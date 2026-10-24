# Independent R1 journal checks

`journal_fold.py` consumes plain row dictionaries. It imports no production
posting, money, canonicalization, database connection or schema helper.

The frozen input is `fixtures/golden/purchase_80.json`, whose digest is recorded in
`fixtures/golden/SHA256SUMS`. It was written before PostgreSQL fixture loading.
Every amount is an integer in **synthetic PLN minor units**: 100 is PLN 1.00.
Actor IDs, account IDs, operation IDs and journal IDs use separate UUID values.
The opening and purchase entries and both projection snapshots are literal data.

## Interfaces

- `fold_entries(accounts, entries)` returns a mapping keyed by
  `(str(workspace_id), str(account_id))` to `Balance(debits, credits, booked)`.
  It includes accounts with no entries and checks each observed journal's debit
  and credit totals. Debit-normal balances subtract credits; credit-normal
  balances subtract debits.
- `assert_projection_matches(accounts, entries, projections)` checks the complete
  account set and every account's debits, credits and booked balance. Missing
  zero-activity rows count as a failure.
- `assert_purchase_matches(request, accounts, journals, entries, before, after)`
  checks the original request's owner/account mapping, operation's exact two
  postings and each projection delta across that isolated purchase. Both snapshots
  include all accounts. Pass the unchanged frozen request, never one reconstructed
  from the resulting journal.

Rows use these field names:

| Row | Required fields |
|---|---|
| Account | `workspace_id`, `id`, `normal_side`; purchase checks also `owner_id`, `class` |
| Entry | `workspace_id`, `journal_id`, `line_no`, `account_id`, `side`, `amount_minor` |
| Journal | `workspace_id`, `journal_id`, `operation_id` |
| Projection | `workspace_id`, `account_id`, `debits`, `credits`, `booked` |
| Request | `workspace_id`, `operation_id`, `customer_id`, `customer_account_id`, `merchant_id`, `merchant_account_id`, `amount_minor` |

UUID objects and strings are supported; projection totals can be integral
`Decimal` values returned by PostgreSQL. Inputs must be a complete, consistent
snapshot. Use plain SELECTs over raw account/journal/entry/projection rows in the
same database snapshot and pass those rows directly to the checks. Database
integration tests own their connections and fixture setup.

The verifier reports mismatching account keys, amounts and fields. The reference
tests deliberately miss the customer projection update, and separately credit M2
in both entries and projection. The latter still passes the fold and is rejected
by the original request's merchant assertion.

## Limits

These are R1 posted-balance and isolated-purchase checks. They do not validate
holds, authorization, journal sealing, commit behavior or concurrency. Empty
journal headers cannot be discovered from an entry-only export and require the
separate real-database/header checks. Passing reference tests is not evidence that
PostgreSQL constraints or production posting are correct.
