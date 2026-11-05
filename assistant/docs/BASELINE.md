# Baseline: three payout incidents without an assistant

Before building anything, this page records how an operator handles three incidents
with what LedgerOps already provides: the HTTP API, the investigation console, the
reconciliation CLI and the docs. Every response quoted here comes from a real run.
[`scenarios/capture.py`](../scenarios/capture.py) builds each incident on disposable
PostgreSQL, drives the real payout code, reads the result through the real Gunicorn
service with scoped tokens, and writes normalized responses to
[`scenarios/snapshots/`](../scenarios/snapshots).

All amounts are PLN minor units. Merchant M1 has 80 and asks for a payout of 60 plus a
fee of 2, so admission holds 62.

| Incident | Question the operator gets | Live API shows | Correct answer |
|---|---|---|---|
| [Limit exceeded](#1-limit-exceeded) | "Why was my payout refused?" | Retained 422 `LIMIT_EXCEEDED`, no hold | Clearing capacity was already committed. The refusal is final for that identity; a new attempt needs a new operation ID and key |
| [Dispatch timeout](#2-dispatch-timeout) | "Was my payout paid?" | `knowledge=UNKNOWN`, hold 62 `HELD`, 1 attempt | Nobody knows yet. Keep the hold, don't resubmit, let recovery look the payment up |
| [Reconciliation mismatch](#3-reconciliation-mismatch) | "Reconciliation exited 1 with MISSING_INTERNAL. What now?" | Same as the timeout | The provider paid; the ledger has not recorded it. Recovery lookup should settle it; nobody posts entries by hand |

## 1. Limit exceeded

**Setup.** M1 holds 62. Merchant M2 has 80 and asks for 60 + 2. The clearing
projection is set to 100, the same deliberately short setup the repository's own
[clearing test](../../tests/integration/test_payout_clearing_commitments.py) uses. A
healthy journal would not produce it by itself.

**What the operator sees.** From [`limit_exceeded.json`](../scenarios/snapshots/limit_exceeded.json):

- `GET /v1/operations/{M2 payout}` → 200 with `admission: REJECTED`, `http_status: 422`,
  `code: LIMIT_EXCEEDED`, `message: "Payout would exceed ledger limits"`, `retryable: false`.
- `GET /v1/payouts/{M2 payout}` → 404. A refused payout has no payout row.
- `GET /v1/metrics` → `unresolved_count: 1`. That is M1's hold, but the response does not say so.
- `GET /v1/investigation/operations/{M1 payout}` shows the 62 hold, if the operator already knows M1's ID.

**Steps today.**

1. Read the retained receipt. `retryable: false` means replaying the same request returns the same 422.
2. Find what `LIMIT_EXCEEDED` means: [PAYOUTS.md, Amounts and admission receipts](../../docs/PAYOUTS.md#amounts-and-admission-receipts)
   (374 words). Admission subtracts clearing's reserved amount and every active `HELD`
   gross amount in the workspace from clearing's booked balance. Unknown and blocked
   payouts still count.
3. Find which holds are using the capacity. There is no HTTP endpoint that lists active
   holds; metrics only counts them. The operator needs an export
   ([EXPORT-AND-VERIFY.md](../../docs/EXPORT-AND-VERIFY.md)) or the payout IDs from
   elsewhere.
4. Decide whether the capacity shortage is real or a projection fault. Here it is a
   fault: two merchants hold 160 against a clearing projection of 100. Independent
   check A would report the drift and [MAINTENANCE.md](../../docs/MAINTENANCE.md)
   covers the repair.
5. Tell the merchant: the refusal is retained. Even after capacity frees up, the same
   operation ID and `Idempotency-Key` return the original 422. A new attempt needs new
   identities ([HTTP-SERVICE.md, API](../../docs/HTTP-SERVICE.md#api)).

**Traps.**

- The code checks five conditions under one error code
  ([`_admission_capacity`](../../src/ledgerops/db/payout_admission.py)): clearing
  capacity, two numeric total limits and two version limits. The docs only describe the
  first. With realistic amounts it is the only one that fails, but the response does not
  say which one did.
- "Retry later" is wrong advice if it means resending the same request.

## 2. Dispatch timeout

**Setup.** M1's payout is admitted. The worker claim commits `MAY_HAVE_SENT`, sends
`PUT`, and the call times out.

**What the operator sees.** From [`dispatch_timeout.json`](../scenarios/snapshots/dispatch_timeout.json):

- `GET /v1/payouts/{id}` → `knowledge: UNKNOWN`, `dispatch_state: OBSERVING`,
  `reservation_state: HELD`, `attempts: "1"`, `recent_observations: []`, `resolution: null`.
- `GET /v1/accounts` → M1 booked 80, reserved 62, available 18.
- `GET /v1/investigation/operations/{id}` → the original 202 receipt, no journal entries,
  hold 62.
- `GET /v1/metrics` → `unresolved_count: 1`, plus the age of the oldest unresolved payout.

**Steps today.**

1. Read the payout state. `UNKNOWN` means the worker may have sent the payment and
   never learned the result.
2. Read [PAYOUTS.md, Dispatch and recovery](../../docs/PAYOUTS.md#dispatch-and-recovery)
   (405 words). A timeout, HTTP 500, `NOT_FOUND` or exhausted attempts cannot release the
   62. After the claim's lease expires, recovery starts with `LOOKUP` under the same
   provider key. Before the dispatch deadline it may resend the original bytes; after
   the deadline it asks the provider to close the payment if it is absent.
3. Tell the merchant the payout is still in progress. Do not resubmit it as a new payout:
   if the first one was paid, a second identity pays twice.
4. Watch `oldest_unresolved_seconds`. If the payout stops progressing, look for an
   incident or `LOCAL_BLOCKER` in the payout state.

**Traps.**

- "It timed out, so it failed" is wrong and expensive.
- Releasing the hold by hand would let the merchant spend money that may already have left.

## 3. Reconciliation mismatch

**Setup.** The same timeout, except the `PUT` did reach the provider and it paid 62.
Only the reply was lost. The provider then issues a signed settlement statement, and
the operator matches it against a ledger export offline.

**What the operator sees.** From
[`reconciliation_missing_internal.json`](../scenarios/snapshots/reconciliation_missing_internal.json):

- The report: `usable: true`, `reconciled: false`, one issue
  `{category: MISSING_INTERNAL, object_id: <payout>, field: journal_count, expected: 1, actual: 0}`,
  a settlement group of 60 + 2 = 62 with `matched: false`, and the payout in
  `unresolved_payout_ids`.
- `GET /v1/payouts/{id}` → **exactly the same as the timeout**: `UNKNOWN`, `HELD`,
  1 attempt, no observations.

**Steps today.**

1. Check the exit code (1: usable input with differences) and read `issues` first, as
   [RECONCILIATION.md, Read the report](../../docs/RECONCILIATION.md#read-the-report)
   (210 words) says.
2. Interpret `MISSING_INTERNAL`: the provider has an effect the ledger has not booked.
   [Time and scope](../../docs/RECONCILIATION.md#time-and-scope) says a provider
   payment with only a local `HELD` state stays `MISSING_INTERNAL`; possible asynchronous
   processing does not hide it.
3. Connect it to the payout: the same ID is `UNKNOWN` in the live API. The statement is
   the only evidence so far that the payment happened.
4. Let recovery do its job. The worker's `LOOKUP` with the original provider key should
   return the signed `SUCCEEDED` result and settle once: merchant 18 / 0 / 18, clearing
   38. The next reconciliation run should then match.
5. If settlement stops with `LOCAL_BLOCKER`, use the two-person
   [operator approval workflow](../../docs/OPERATOR-ACTIONS.md). The report itself
   "cannot authorize a release, financial correction or external payment".

**Traps.**

- Posting a manual journal to make reconciliation pass would book the payment a second
  time when recovery settles it.
- The report says what differs, not who is right. The categories are "investigation
  categories, not automatic verdicts".

## What the baseline says about an assistant

1. **Docs explain rules; the API reports facts.** Each incident needed one or two doc
   sections of 200–400 words, out of about 21,000 words in `docs/`. Finding
   the right section is where retrieval can help.
2. **Docs cannot answer "was it paid?"** Incidents 2 and 3 look identical in the live API.
   Only provider evidence (a lookup or a signed statement) tells them apart. A docs-only
   assistant must say it cannot know, and later, a tool-using one must say which evidence
   it read.
3. **The dangerous answers are actions:** resubmitting a payout, releasing a hold,
   posting a correcting entry. The assistant explains and drafts; it never approves or
   executes. Approval stays in the existing two-person workflow.
4. **Some gaps are API gaps, not knowledge gaps.** No endpoint lists the holds that use
   clearing capacity, and `LIMIT_EXCEEDED` does not say which check failed. An assistant
   should report these gaps rather than guess.

## Reproduce

From the repository root, with Docker running:

```sh
uv run --locked python -m pytest -c pyproject.toml -p tests.conftest -q assistant/scenarios/capture.py
```

`LEDGEROPS_SCENARIOS_CHECK=1` compares a fresh capture with the committed snapshots
instead of rewriting them; CI runs it that way. Run-specific values (database times,
random request IDs, digests, retry jitter) become numbered placeholders, so equal values
keep equal placeholders. Times from the fixed scenario clock (2026-10-29) are kept.
