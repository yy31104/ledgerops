# Assistant retrieval database

The assistant owns a separate database. Its migration account may install pgvector;
the runtime login receives only USAGE on `assistant_rag` and SELECT on its tables.
The product additionally grants CRUD on its dedicated workflow/checkpoint tables;
it grants no financial database access.
No ledger database login belongs in this package. SQL lives in the package as forward
migrations; never edit a migration after it has been released.

`001_rag.sql` creates immutable corpus snapshots and 384-dimensional chunk vectors,
an English tsvector with GIN, and a cosine HNSW index (m=16, ef_construction=64).
The snapshot key hashes the document digest and pinned embedding identity. Indexing
embeds outside the database transaction, then publishes the snapshot atomically.
Concurrent indexers use the same key and cannot publish half a corpus.

Keyword retrieval uses PostgreSQL `websearch_to_tsquery` with OR between normalized
query terms and `ts_rank_cd`. This is PostgreSQL full-text search, not BM25. HNSW uses
ef_search=100 and strict iterative scans to compensate for filtering by corpus.
PostgreSQL may prefer a sequential scan for a small corpus; an available index does
not prove every query used it. The integration test checks the HNSW execution plan.
An exact pgvector path disables index scans only in its own read-only transaction,
giving an independent recall reference. RRF combines keyword and vector ranks with
k=60 and depth=50, without comparing incompatible raw scores.

Run a complete local experiment without configuring credentials:

```sh
cd assistant
uv sync --locked
uv run --locked python scripts/evaluate_local.py --split dev --output .cache/report.json
uv run --locked python -m pytest -m postgres
```

The script creates a named, isolated Docker container with a random temporary
password and removes only that container. It records every experiment in local
MLflow SQLite under `.cache/mlflow`; nothing is sent to a tracking service.
Open its UI with an independently installed MLflow server pointed at that database,
or read runs through `MlflowClient`.

For a persistent local assistant DB, set `LEDGEROPS_ASSISTANT_DATABASE_URL` in the
environment. Run schema setup and indexing using the owner before granting runtime
SELECT. `LEDGEROPS_ASSISTANT_CORPUS_ROOT` points installed packages at the repository.

References: [pgvector](https://github.com/pgvector/pgvector),
[PostgreSQL search](https://www.postgresql.org/docs/current/textsearch-controls.html),
[MLflow tracking](https://mlflow.org/docs/latest/ml/tracking/).
