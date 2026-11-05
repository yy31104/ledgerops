# LedgerOps

![LedgerOps — A timeout is not a failed payment.](https://readme-typing-svg.demolab.com?font=JetBrains+Mono&size=24&duration=2800&pause=1100&color=36B7A8&width=720&height=70&lines=LedgerOps;A+timeout+is+not+a+failed+payment.)

![Python 3.12](https://img.shields.io/badge/Python-3.12-36B7A8?style=flat-square&labelColor=17202A&logo=python&logoColor=white)
![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-36B7A8?style=flat-square&labelColor=17202A&logo=postgresql&logoColor=white)
[![CI](https://github.com/yy31104/ledgerops/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yy31104/ledgerops/actions/workflows/ci.yml?query=branch%3Amain)

**A balanced ledger can still pay the wrong merchant.**

LedgerOps is a Python + PostgreSQL ledger for a small payment platform: customers
make purchases and merchants withdraw their balance. It follows three troublesome
cases: two requests spending the same money, a worker crashing just after the
provider pays, and balanced entries crediting the wrong merchant.

Solo portfolio project. The payment provider is a local simulator; no real money moves. Built with AI assistance.

[Run locally](#run-locally) · [Docs](#docs)

## How a payout flows

For a payout of 60 with a fee of 2:

1. `POST /v1/payouts` reaches [`db/payout_admission.py`](src/ledgerops/db/payout_admission.py), which locks clearing and merchant balances, reserves 62 and returns 202.
2. [`worker.py`](src/ledgerops/worker.py) and [`db/payout_dispatch.py`](src/ledgerops/db/payout_dispatch.py) commit `MAY_HAVE_SENT` before making the call: the provider may receive it even if the worker never sees a reply.
3. [`provider_sim/`](provider_sim) stores the payment and its result under a persistent payment key.
4. [`db/payout_finance.py`](src/ledgerops/db/payout_finance.py) consumes the hold on success or releases it on confirmed failure. An unknown outcome keeps the money reserved.

A timeout never releases money; the provider must confirm that the payment failed or was closed without paying.

Amounts below are PLN minor units: 100 means PLN 1.00. Booked is the merchant's recorded balance; reserved is money held for a payout.

| Step | Customer | Merchant booked / reserved / available |
|---|---:|---|
| Open accounts | 100 | 0 / 0 / 0 |
| Purchase 80 | 20 | 80 / 0 / 80 |
| Withdraw 60 + fee 2; outcome pending | 20 | 80 / 62 / 18 |
| Provider confirms success | 20 | 18 / 0 / 18 |

## Failure cases

| What goes wrong | What happens | Test |
|---|---|---|
| Two purchases of 80 compete for 100 | One commits; the other receives a retained 422 refusal | [Concurrent spending](tests/integration/test_purchase_concurrency.py) |
| Provider pays, then the worker is killed | Recovery queries the original payment identity and settles once | [Process recovery](tests/integration/test_payout_worker_process.py) |
| Entries balance but credit the wrong merchant | Independent request checks detect the wrong owner and block rebuild | [Wrong-owner check](tests/integration/test_export_verification.py) |
| Two merchants promise more than clearing can pay | Admission counts existing holds before accepting another payout | [Shared capacity](tests/integration/test_payout_clearing_commitments.py) |
| A backup predates a completed provider payment | Recovery reads provider history, keeps runtime access closed and revokes old credentials | [Backup recovery](tests/integration/test_backup_recovery.py) |

The first and fourth rows are the attack class that [ACIDRain](https://doi.org/10.1145/3035918.3064037)
(Warszawski and Bailis, SIGMOD 2017) describes: concurrent requests through a normal API
each pass a balance or capacity check before the other commits, so together they spend
more than exists. The paper found 22 such vulnerabilities in 12 e-commerce applications,
mostly from mis-scoped transactions and default isolation levels. Here the tests do not
rely on timing: a third session holds the balance row, both requests start in their own
PostgreSQL sessions, and `pg_blocking_pids` confirms each is waiting on the same
`FOR UPDATE` lock before it is released.

## Project layout

| Path | What | Start here |
|---|---|---|
| `src/ledgerops/http/` | Authenticated API and investigation console | [`application.py`](src/ledgerops/http/application.py) |
| `src/ledgerops/db/` | Admission, posting, holds and settlement | [`admission.py`](src/ledgerops/db/admission.py) |
| `src/ledgerops/` | Payout worker and independent verification | [`worker.py`](src/ledgerops/worker.py), [`verification.py`](src/ledgerops/verification.py) |
| `provider_sim/` | Separate HTTP provider with persistent SQLite storage | [`store.py`](provider_sim/store.py) |
| `tests/integration/` | Database contention, process failures and recovery | [`test_payout_worker_process.py`](tests/integration/test_payout_worker_process.py) |
| `ops/linux/` | TLS, role-separated containers and operator commands | [`README.md`](ops/linux/README.md) |

## Run locally

Install Python 3.12, [uv](https://docs.astral.sh/uv/), `make` and Docker, then clone the repository:

```sh
git clone https://github.com/yy31104/ledgerops.git
cd ledgerops
make bootstrap
```

Run the narrated ledger demo. It creates temporary PostgreSQL, walks through purchase,
reversal, drift detection and repair, then cleans up its resources:

```sh
make demo
```

For the authenticated service and payout flow, follow the [Linux setup](ops/linux/README.md). That path provisions the provider, API, worker and separate operator instance.

Run the full suite with Docker available; allow about eight minutes:

```sh
make test
```

Check formatting and the installed package:

```sh
make check
```

## Design notes

**Keep the transaction visible.** Python coordinates each business operation through
explicit SQL. Ordered row locks serialize competing balances, while the request key
and business ID retain the original accepted or rejected result for retries.

**Check intent as well as arithmetic.** One independent check folds journal entries
and hold history into balances; another compares actual effects with the saved
request. This catches a transfer whose journal and balance projection agree on the
wrong merchant.

**Recover from retained facts.** The worker saves its dispatch intent before network
I/O and preserves the provider key across restarts. Repair recomputes projections
after blocking new writes and waiting for in-flight transactions to finish.
Operator actions bind approval to current facts.

## Limits

- The provider is a simulator; it retains payment keys and records closed payments to reject late requests.
- Deployment has been exercised on one Linux host; cloud operation and high availability are untested.
- No real bank is connected.

## Docs

- [Service setup and operation](docs/RELEASE-READINESS.md)
- [HTTP API and authentication](docs/HTTP-SERVICE.md)
- [Projection repair](docs/MAINTENANCE.md)
- [Backup and restore](docs/BACKUP-RESTORE.md)
- [Approval workflow](docs/OPERATOR-ACTIONS.md)
