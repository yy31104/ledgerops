# LedgerOps operator assistant

An assistant for the person on call, not for the ledger. It answers questions such as
"why was this payout refused?" or "was it paid?" by citing the LedgerOps docs and, in a
later step, by reading the existing read-only API. It never approves, executes or
releases anything; those stay in the two-person approval workflow.

It lives in this repository as a separate package so it can read the docs and replay
real incidents, without adding dependencies to the ledger itself.

| Path | What |
|---|---|
| [`docs/BASELINE.md`](docs/BASELINE.md) | How an operator handles three incidents today, without AI |
| [`scenarios/capture.py`](scenarios/capture.py) | Reproduces those incidents on real PostgreSQL and Gunicorn |
| [`scenarios/snapshots/`](scenarios/snapshots) | What the operator sees in each one, normalized |

Regenerate the snapshots from the repository root with Docker running:

```sh
uv run --locked python -m pytest -p tests.conftest -q assistant/scenarios/capture.py
```
