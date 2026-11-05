# Client histories and an offline isolation checker

This directory records what concurrent admission transactions **read and wrote, as the
client saw it**. It then checks those histories offline for isolation anomalies,
following Elle (Kyle Kingsbury and Peter Alvaro, *Elle: Inferring Isolation Anomalies
from Experimental Observations*, PVLDB 14(3), 2020). See
[ADR 0005](../../docs/adr/0005-client-history-checking.md) for the decision.

| File | Role |
|---|---|
| `checker.py` | Pure-stdlib checker for a JSON Lines history; also a CLI |
| `recorder.py` | psycopg cursor installed via `cursor_factory`; `src/ledgerops` is unchanged |
| `conftest.py` | Overrides `db`/`seeded_db` so every test here records, then checks at teardown |
| `test_checker.py` | Hand-written histories: one positive case per anomaly, plus serial histories that must pass |
| `test_existing_concurrency.py` | Imports 7 existing concurrency tests (11 cases) and reruns them unchanged with recording |
| `test_lock_mutant.py` | Strips `FOR UPDATE OF b` and shows the checker catching the race; locked control |

## What is recorded

Purchase and reversal admission share two balance statements. For each attempt the
recorder writes:

- `["r", account, version]`: the balance row the code actually fetched under `FOR UPDATE OF b`.
- `["w", account, version]`: the version its relative `UPDATE … version=version+1` installed,
  read back inside the same transaction while it still holds the row lock.

`version` increases by exactly one per write, so `(account, version)` names a single
write and the version order is known. This is what Elle needs in order to infer
dependencies. Elle gets it from list-append or unique register writes; here the
version column provides it.

An attempt starts at admission's `SET TRANSACTION ISOLATION LEVEL`, so internal
40P01/40001 retries are separate attempts. Status comes from
`pg_xact_status(xid)` after the test: `ok` (committed), `fail` (aborted) or `info`
(unknown).

## What is checked

| Anomaly | Meaning here |
|---|---|
| G0 | Write cycle (ww edges only) |
| G1a | A committed transaction read a version written only by an aborted one |
| G1b | It read a version its writer later overwrote in the same transaction |
| G1c | Cycle of ww/wr edges |
| G-single | Cycle with exactly one anti-dependency (rw) edge, e.g. read skew or a hidden lost update |
| G2-item | Cycle with two or more rw edges, e.g. write skew |
| lost-update | Two committed transactions read the same version of a key and both wrote it |
| non-adjacent-rmw | A transaction read version *v* but installed something other than *v+1* |
| duplicate-version | Two committed transactions installed the same version |

## Run

```bash
uv run --locked python -m pytest tests/history
```

Keep the histories and check one offline:

```bash
LEDGEROPS_HISTORY_DIR=/tmp/ledgerops-histories uv run --locked python -m pytest tests/history
```

```bash
uv run --locked python -m tests.history.checker /tmp/ledgerops-histories/FILE.jsonl
```

The CLI prints a JSON report and exits with 1 if it finds anomalies.

## Break it

1. In `test_lock_mutant.py`, the 30 + 30 purchase race with the lock stripped still ends
   at customer 40 and merchant 60, and the independent fold agrees. The history shows
   both transactions read customer v1 and merchant v0, so the checker reports
   `lost-update`, `non-adjacent-rmw` and `G-single`. Business totals alone cannot
   see this.
2. The 80 + 80 race with the lock stripped is stopped by `CHECK (booked >= reserved)`.
   The committed history is clean, but the loser raises `CheckViolation` instead of
   committing the retained 422 refusal that the real implementation produces.
3. To see the guard against vacuous recording, change `WRITE_MARK` in `recorder.py` to
   any other text and rerun `test_existing_concurrency.py`. Each teardown fails with
   "observed no committed balance transaction" or reports reads with no writes. A
   recorder that silently stops matching cannot pass as "no anomalies".

## Limits

- Only `ledger.account_balance` rows touched by purchase and reversal admission are
  recorded. Payout holds (`reserved`), journals and identity rows are not modelled.
- Statements are recognised by SQL text. If admission's SQL changes, the
  "committed >= 1" teardown assertion fails instead of silently passing.
- Versions written by fixture code are not recorded; they act as external writes.
  Across such gaps, edges are still valid ordering constraints, but the edge type
  may be approximate.
- The checker targets serializability anomalies only, not real-time order (strict
  serializability).
- `test_existing_concurrency.py` imports test functions from `tests/integration/`. A
  rename there fails at collection with an ImportError; it does not silently drop
  coverage.
