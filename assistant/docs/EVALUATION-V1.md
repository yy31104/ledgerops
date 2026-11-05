# PostgreSQL retrieval comparison (v1 development set)

This is a real local bge-small / PostgreSQL 18 / pgvector 0.8.7 run. Generation uses the fake provider. The 60-question set is automatically frozen with owner review deferred; 40 development questions contain 32 answerable retrieval cases. The 20-question holdout has not been used to tune settings.

| Method | Recall@5 | MRR@10 | nDCG@5 | Mean ms |
|---|---:|---:|---:|---:|
| postgres_fts | 0.6250 | 0.6084 | 0.5520 | 9.6 |
| pgvector_hnsw | 0.7344 | 0.6687 | 0.6253 | 11.6 |
| pgvector_exact | 0.7344 | 0.6687 | 0.6253 | 11.4 |
| hybrid | 0.7083 | 0.7527 | 0.6741 | 21.4 |

Hybrid improves first-rank placement in this development run, but has lower Recall@5 than vector retrieval. No claim that fusion wins every metric is justified. Exact pgvector and the HNSW-configured path return the same rankings for this small corpus; the planner may select a sequential scan. The index plan itself is exercised independently in the PostgreSQL tests.

The earlier BM25 results in EVALUATION.md are a separate experiment and are not renamed PostgreSQL FTS. The full v1 run and its MLflow run ID are in ../eval/results/v1-postgres-dev.json. Settings: RRF k=60, depth=50; HNSW ef_search=100, m=16, ef_construction=64. Binary nDCG is over distinct relevant sections.

CI evaluates the development split with real pinned embeddings and fake generation. It fails if Recall@5 falls below the committed baseline for FTS, vectors or hybrid. Changing the labels, embedding identity or substituting hashing vectors also fails the gate. Test values are never used as a quality threshold.

Known limitations: all labels are synthetic and agent-authored, English-only, and not manually validated. Refusal/routing tests do not establish real-model answer quality. Citation validation establishes that IDs were supplied to the model, not that the prose is semantically entailed. No paid or subscription inference was run.

See POSTGRES.md for reproduction and the local MLflow store.
