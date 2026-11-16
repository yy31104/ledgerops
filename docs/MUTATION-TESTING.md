# What breaks when a guard disappears

Predict the outcome before opening the table. Then run one named variant:

```sh
make mutant NAME=timeout-hold
# Run every selected guard and its pristine control:
make mutant NAME=all
```

A Git checkout, Docker, Python 3.12 and uv are required. The runner copies repository files into a
new temporary directory for each trial. It imports that copy's `src`, changes only
that copy, and runs the same focused test before and after the edit. PostgreSQL is
disposable; only containers carrying that trial's generated label can be removed.
The original `src` digest must match before and after. No migration is edited in Git.

| Rule / name | What the variant changes or exposes | Test that detects it |
|---|---|---|
| Durable send intent / `claim-before-commit` | Transport enters the claim transaction before commit; its boundary is no longer idle/durable. | [Committed claim before transport](../tests/integration/test_payout_dispatch.py#L102) |
| Stable payment identity / `provider-key` | A fresh, well-formed key replaces the stored key at transport; retries cease to name the same payment. | [Exact original identity on resend](../tests/integration/test_payout_dispatch.py#L389) |
| Timeout reservation / `timeout-hold` | A timeout decreases reserved funds although the provider outcome remains unknown. | [Transport failures keep the hold](../tests/integration/test_payout_dispatch.py#L268) |
| Claim lease / `claim-lease` | A second worker can reclaim before `next_due_at`. | [Abandoned claim recovery waits](../tests/integration/test_payout_dispatch.py#L153) |
| Terminal observation replay / `observation-replay` | Both duplicate-event early returns are bypassed. The settlement hold guard still prevents another payment effect, but the API wrongly returns BLOCKED instead of REPLAYED. | [Repeated event across channels](../tests/integration/test_payout_resolution.py#L96) |
| Balance row locks / `balance-lock` | Admission reaches an UPDATE without taking the required balance read lock; the observed wait is at the wrong statement. | [Two competing debits](../tests/integration/test_purchase_concurrency.py#L126) |
| Projection floor / `balance-floor` | The database can accept reserved funds above booked funds. | [Real projection row constraints](../tests/integration/test_schema.py#L47) |
| Scoped request uniqueness / `idempotency-key` | Adding operation ID to the key lets a second operation claim it. A later immutability guard stops replacement, but the caller gets a constraint error instead of the required 409. | [Changed operation under one key](../tests/integration/test_purchase_admission.py#L125) |

**Result: 8 selected rules, 8 detected, no survivors or invalid trials.** Every
pristine control passed. One financial regression test was added: the stored-key
failure mode was previously uncovered by a direct identity assertion. The earlier
resend test failed only indirectly with an AttributeError before reaching its
identity comparison. It is retained; the new test checks every transport key
against the stored row explicitly. The runner has 44 safety cases.

A kill requires a passing control, matching test IDs and that case's registered
semantic assertion or reviewed database-guard error. Generic assertions, programming
exceptions, helper watchdog failures, skips, fixture/collection errors, malformed
JUnit and runner timeouts are invalid evidence. A per-copy exclusive marker makes
patch application one-shot, including patches whose original anchor remains. Removing one guard
can expose another: the table does not claim every variant moves money twice.
This is a selected guard check, not a score over all possible implementation bugs.

The command lists failed test IDs. A per-file SHA-256 manifest accompanies the
raw patches, source/runner hashes, JUnit and failure output under
ignored `.cache/mutants/`. The v1.1.0 release carries the raw records; Git contains
this summary and the runner. CI executes the matrix and retains its raw artifact.
Existing evidence directories are never overwritten. Exit 0 means every selected
variant was detected; 1 means a survivor; 2 means invalid evidence or invocation.
