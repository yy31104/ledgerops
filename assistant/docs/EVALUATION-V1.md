# PostgreSQL retrieval comparison (v1)

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

## Held-out run

After freezing the corpus, labels and development-selected settings, the 20 held-out
questions were evaluated once. Twelve are scored document-retrieval cases; the
remaining eight exercise refusal/routing. Human review of the agent-authored labels
was explicitly deferred. No settings were changed in response to these scores.

| Method | Recall@5 | MRR@10 | nDCG@5 | Mean ms |
|---|---:|---:|---:|---:|
| postgres_fts | 0.4167 | 0.1944 | 0.2494 | 10.7 |
| pgvector_hnsw | 0.5000 | 0.3750 | 0.4129 | 15.0 |
| pgvector_exact | 0.5000 | 0.3750 | 0.4129 | 16.7 |
| hybrid | 0.3333 | 0.2989 | 0.2718 | 26.3 |

Hybrid generalizes poorly on this sample and underperforms vectors on every reported
quality metric. The corpus has only 175 chunks, so equal exact/HNSW scores are not
evidence of approximate-search performance at scale. Timings are local wall-clock
retrieval measurements, not service latency or a hardware-independent benchmark.

Fake generation routes 17/20 cases as labelled (0.85). It correctly refuses 5/8
refusal cases and misses three: two out-of-scope questions and one live-state
question. Relevant-section citation hit rate is 2/12 (0.1667). These are failures,
not a demonstration of reliable answering. The lexical/similarity gate and fake
sentence copier do not establish real-model injection resistance or factual
accuracy. The graph's closed capabilities and upstream scopes are tested separately.

The raw report is [v1-postgres-holdout.json](../eval/results/v1-postgres-holdout.json),
including per-question rankings, answers, corpus digest, label SHA and MLflow run ID.
It remains an assessment of the frozen retrieval/RAG baseline, not a new evaluation
of the product's live API agent. The earlier development-only scope-gate calibration
has no held-out gate-accuracy value; `null` means unmeasured, not zero.

This sample is too small and its labels insufficiently independent for a production
quality claim. A later improvement should use reviewed independent labels and a new
unseen test set. CI deliberately gates development Recall, not these exposed
held-out scores. No paid API or subscription inference was executed.
