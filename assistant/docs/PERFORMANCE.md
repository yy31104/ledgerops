# Retrieval performance baseline

Measured on 2026-11-11, using main `415faba`'s 21 files and 179 chunks.
Corpus digest: `6d7831b77af0d596e4693f06c827abcf75d6b79cfee1be369d27b9e96be00e91`.

This is an isolated measurement harness. Retrieval, context, corpus, embeddings,
baselines, dependencies and the CI gate are unchanged. Only the 32 existing dev
documentation questions are queried. Neither v2 nor v3 is evaluated; v1-011 remains
unresolved. No answer model or paid inference API is called.

## Records and provenance

Each scale has a cold-encoding record and two validated retrieval repeats. Cold
encoding was captured with harness `ed92b0d`; its query/ANN results are excluded.
All original query/quality records use `a3b38b3`, which verifies actual HNSW plans.
The corpus, embedding and retrieval implementations are identical in both commits.
Each original JSON contains SHA-256 hashes of seven measured source modules.
The separate attribution record binds its published code block and the six input
records by SHA-256; it does not replace any historical measurement.

A preliminary plan check exposed a measurement trap: disabling sequential scans
alone selected a primary-key bitmap scan, which is still exact. Dedicated ANN
recall now disables sequential scans, bitmap scans and explicit sorting, then
rejects results unless EXPLAIN names `chunks_embedding_idx`. Timed
default-planner queries retain production settings; these extra flags affect only
the separate ANN measurement, never application code.

| N synthetic chunks | Cold encoding | Retrieval repeat 1 | Retrieval repeat 2 |
| --- | --- | --- | --- |
| 1000 | [JSON](../perf/results/1000-cold-encoding.json) | [JSON](../perf/results/1000-run1.json) | [JSON](../perf/results/1000-run2.json) |
| 10000 | [JSON](../perf/results/10000-cold-encoding.json) | [JSON](../perf/results/10000-run1.json) | [JSON](../perf/results/10000-run2.json) |

100,000 chunks: **not run**. The CLI requires --allow-large at that scale.
There is no 100k latency, memory, quality or extrapolated runtime claim.

## Workload and measurement boundaries

sentence-mix-v1 uses seed 20261111. It splits the 179 real passages into sentences,
mixes their tokens, and cycles the empirical full-text word counts with a sampled
remainder. Every generated chunk has its own virtual document/section ID.
This preserves word lengths, not realistic topic structure, code fences or token
lengths. Synthetic passages never enter an operating corpus or baseline.

Latency, ingestion and index sizes use exactly N generated chunks. Rechunking the
N virtual Markdown documents is a separate workload and can produce more than N
chunks; that output is discarded. Quality checks later replace the disposable
database with the 179 real chunks, then with real + N distractors.

Every run owns a pinned, loopback-only pgvector container and deletes it afterward.
There is no developer/production DSN option; only this owned database is truncated.
Passage-vector caches and intermediate artifacts stay in .cache, outside Git.
Raw EXPLAIN plans can include the encoded query-vector literal.

Generation/chunking warm up on up to 20 examples; BGE warms on 16 passages.
Database setup writes 20 rows with both secondary indexes, then truncates and
drops those indexes. Timed ingestion uses the original index_corpus implementation,
cached vectors and its primary key, without GIN/HNSW. Both indexes are then
built and timed separately using their original parameters.

Each latency method warms on 16 searches, then executes 200 sequential searches
with top-k 10. The query pack cycles the 32 dev questions and shuffles with the
seed: these are repeated questions, not 200 distinct queries. perf_counter-only
timing includes query encoding where applicable, fresh PostgreSQL connections,
SQL, decoding and result construction. BM25/FTS do not encode queries. Hybrid
uses depth 50, k=10 fusion and its two sequential database searches.

Percentiles interpolate sorted raw samples at (n-1)*p. QPS is 200 divided by total
measured search duration, excluding warmup/profiling. Other phase timings include
tracemalloc overhead. Two repeats describe noise, not a confidence interval.

## Environment

| Item | Recorded value |
| --- | --- |
| CPU / host RAM | Apple M4 Pro, 14 logical / 14 physical cores; 24.00 GiB |
| OS / Python | Darwin 27.2.0 arm64; 3.12.13 |
| NumPy / ONNX Runtime | 2.5.4 / 1.31.1 |
| PostgreSQL / pgvector | PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit; pgvector 0.8.7 |
| Docker | Desktop Linux VM, aarch64, 14 CPUs, 7.75 GiB; Engine 29.4.3 |
| PostgreSQL settings | shared_buffers=128MB, work_mem=4MB, maintenance_work_mem=64MB, max_parallel_workers_per_gather=2 |
| Threads | onnx_intra_op=4, OMP_NUM_THREADS=unset, OPENBLAS_NUM_THREADS=unset, MKL_NUM_THREADS=unset, VECLIB_MAXIMUM_THREADS=unset |

Dependencies come from unchanged assistant/uv.lock. Pinned BGE weights were
already cached; passage vectors were initially uncached. The existing BGE encoder
uses CLS pooling, L2 normalization, batch size 16 and four intra-op threads.
This laptop/Docker setup is not a production or concurrency benchmark.

## Preparation, storage and token lengths

Validated repeats reuse passage-vector caches but regenerate passages, recreate the
database, and write/build indexes. Cache hits are not encoding throughput.
Cold records provide the real encoding measurement.

| N | Repeat 1 total s | Repeat 2 total s | Cold BGE s | Cold BGE chunks/s | Cold encoding worker s |
| --- | --- | --- | --- | --- | --- |
| 1000 | 44.85 | 45.46 | 29.62 | 33.77 | 37.43 |
| 10000 | 100.45 | 103.46 | 296.11 | 33.77 | 300.04 |

Cold BGE seconds exclude model loading, warmup, token-length analysis, real-corpus
cache preparation and profiling; encoding-worker wall time includes those costs.
Total seconds are end-to-end times for the validated, cached repeats.
The following preparation/storage tables use repeat 1; repeat 2 retains all
corresponding measurements in JSON.

| N | Generate chunks/s | Rechunk output | Rechunk chunks/s | PG write s | GIN build s | HNSW build s |
| --- | --- | --- | --- | --- | --- | --- |
| 1000 | 17174 | 1066 | 10487 | 0.461 | 0.031 | 0.152 |
| 10000 | 29458 | 10684 | 10015 | 4.492 | 0.140 | 0.845 |

| N | Table main fork bytes | GIN bytes | HNSW bytes | Table + TOAST + all indexes bytes |
| --- | --- | --- | --- | --- |
| 1000 | 1662976 | 532480 | 2056192 | 7847936 |
| 10000 | 16457728 | 3522560 | 20488192 | 75448320 |

Main forks use pg_relation_size. The total uses pg_total_relation_size after both
indexes exist, including the primary key, TOAST and other forks; it is not the sum
of only the preceding columns. The GIN record's earlier total predates HNSW.
[PostgreSQL size functions](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADMIN-DBSIZE).

Untruncated BGE lengths include special tokens. A cloned tokenizer disables
padding/truncation for measurement; the encoder's 512-token limit stays unchanged.

| Population | Mean tokens | p50 | p95 | p99 | Max | >512 |
| --- | --- | --- | --- | --- | --- | --- |
| Real 179 | 241.98 | 255.0 | 413.29999999999995 | 540.0799999999999 | 614 | 3 |
| Synthetic 1000 | 244.85 | 267.0 | 393.0 | 430.01 | 472 | 0 |
| Synthetic 10000 | 245.10 | 262.0 | 390.0 | 430.0 | 539 | 5 |

The real passages silently truncated by ordinary BGE embedding are:

| Chunk ID | Untruncated tokens |
| --- | --- |
| docs/RELEASE-READINESS.md#running-the-project/0 | 537 |
| ops/linux/README.md#backup-restore-and-reopening/0 | 551 |
| ops/linux/README.md#backup-restore-and-reopening/4 | 614 |

Word-count matching does not establish token-length matching. Synthetic passages
lose the original CLI/code structure, so the real-corpus truncation check remains
necessary. Full integer length histograms are in JSON.

## Warm query latency and repeatability

Repeat 1, milliseconds; every row retains all 200 raw samples. HNSW rows denote
the configured production-style query. At 1k the planner actually selects a
sequential scan for all ef values; at 10k it uses HNSW. The 1k latency rows must
not be paired with forced-ANN recall as if both measured an HNSW traversal.

| N | Method | p50 ms | p95 ms | p99 ms | Single-client QPS |
| --- | --- | --- | --- | --- | --- |
| 1000 | memory_bm25 | 0.528 | 0.770 | 0.793 | 1774.98 |
| 1000 | memory_vector | 2.382 | 2.597 | 3.647 | 411.14 |
| 1000 | postgres_fts | 11.113 | 14.169 | 19.707 | 88.81 |
| 1000 | postgres_exact | 11.746 | 13.283 | 19.701 | 82.95 |
| 1000 | hnsw_40 | 11.638 | 13.353 | 14.268 | 84.81 |
| 1000 | hnsw_100 | 11.732 | 13.847 | 16.847 | 82.90 |
| 1000 | hnsw_200 | 11.683 | 13.148 | 14.811 | 84.68 |
| 1000 | hybrid_10 | 21.883 | 25.236 | 27.653 | 44.97 |
| 10000 | memory_bm25 | 9.477 | 12.798 | 13.485 | 101.32 |
| 10000 | memory_vector | 3.015 | 3.307 | 4.136 | 327.87 |
| 10000 | postgres_fts | 42.140 | 57.669 | 61.373 | 22.93 |
| 10000 | postgres_exact | 29.370 | 31.498 | 33.417 | 33.70 |
| 10000 | hnsw_40 | 10.951 | 12.675 | 14.670 | 89.59 |
| 10000 | hnsw_100 | 11.450 | 13.343 | 16.993 | 85.55 |
| 10000 | hnsw_200 | 12.101 | 13.423 | 13.945 | 81.38 |
| 10000 | hybrid_10 | 55.386 | 73.314 | 77.778 | 17.52 |

Same command, second invocation; delta = (repeat 2 / repeat 1 - 1) * 100:

| N | Method | Repeat 1 p50 ms | Repeat 2 p50 ms | Delta % |
| --- | --- | --- | --- | --- |
| 1000 | memory_bm25 | 0.528 | 0.540 | +2.18 |
| 1000 | memory_vector | 2.382 | 2.381 | -0.06 |
| 1000 | postgres_fts | 11.113 | 11.210 | +0.87 |
| 1000 | postgres_exact | 11.746 | 11.972 | +1.92 |
| 1000 | hnsw_40 | 11.638 | 11.875 | +2.04 |
| 1000 | hnsw_100 | 11.732 | 11.697 | -0.30 |
| 1000 | hnsw_200 | 11.683 | 11.918 | +2.01 |
| 1000 | hybrid_10 | 21.883 | 22.156 | +1.25 |
| 10000 | memory_bm25 | 9.477 | 9.001 | -5.02 |
| 10000 | memory_vector | 3.015 | 2.948 | -2.24 |
| 10000 | postgres_fts | 42.140 | 46.148 | +9.51 |
| 10000 | postgres_exact | 29.370 | 30.408 | +3.54 |
| 10000 | hnsw_40 | 10.951 | 11.551 | +5.47 |
| 10000 | hnsw_100 | 11.450 | 11.987 | +4.68 |
| 10000 | hnsw_200 | 12.101 | 12.898 | +6.59 |
| 10000 | hybrid_10 | 55.386 | 55.905 | +0.94 |

## Memory

Every stage/method has a fresh subprocess. tracemalloc measures allocations inside
its marked phase, not inputs/imports allocated earlier. ONNX native allocations
require RSS. RSS is that worker's lifetime high-water mark, including setup,
warmup and profiles, not a phase delta or a simultaneous system-wide peak.
Generation and chunking share a worker, so their RSS marks are cumulative.

All query workers load BGE, including lexical methods: RSS is not standalone
BM25/FTS memory. PostgreSQL-server/VM memory is not included in client RSS.
[tracemalloc](https://docs.python.org/3.12/library/tracemalloc.html),
[resource.getrusage](https://docs.python.org/3/library/resource.html#resource.getrusage).

Phase allocation peaks and worker RSS below are MiB. Encoding uses the cold
record; other rows use repeat 1. Initialization/cache-load details remain in JSON.

| N | Phase | Traced Python peak MiB | Worker RSS peak MiB |
| --- | --- | --- | --- |
| 1000 | Generate | 3.265 | 52.16 |
| 1000 | Rechunk | 0.029 | 52.25 |
| 1000 | Cold BGE | 3.294 | 1289.06 |
| 1000 | PG write | 8.164 | 63.12 |
| 1000 | GIN build | 0.029 | 54.47 |
| 1000 | HNSW build | 0.029 | 54.69 |
| 1000 | ANN/dev/profile phase | 12.226 | 326.66 |
| 10000 | Generate | 18.884 | 74.52 |
| 10000 | Rechunk | 0.037 | 74.67 |
| 10000 | Cold BGE | 32.145 | 2000.61 |
| 10000 | PG write | 80.976 | 210.41 |
| 10000 | GIN build | 0.029 | 128.22 |
| 10000 | HNSW build | 0.029 | 128.25 |
| 10000 | ANN/dev/profile phase | 101.589 | 603.11 |

Query memory is a separate post-latency search; tracing is off during latency
samples. Retriever initialization follows corpus/vector/model loading. A small
vector-retriever initialization peak does not mean the preloaded matrix is free.

| N | Method | Retriever init Python MiB | One-query Python KiB | Worker RSS MiB |
| --- | --- | --- | --- | --- |
| 1000 | memory_bm25 | 8.374 | 188.95 | 293.88 |
| 1000 | memory_vector | 0.009 | 26.61 | 282.62 |
| 1000 | postgres_fts | 0.006 | 29.64 | 282.92 |
| 1000 | postgres_exact | 0.006 | 49.86 | 285.95 |
| 1000 | hnsw_40 | 0.006 | 49.37 | 286.00 |
| 1000 | hnsw_100 | 0.006 | 49.37 | 286.05 |
| 1000 | hnsw_200 | 0.006 | 49.31 | 314.61 |
| 1000 | hybrid_10 | 0.006 | 252.53 | 286.20 |
| 10000 | memory_bm25 | 80.547 | 1957.97 | 482.91 |
| 10000 | memory_vector | 0.082 | 202.43 | 352.64 |
| 10000 | postgres_fts | 0.007 | 35.22 | 352.72 |
| 10000 | postgres_exact | 0.007 | 50.08 | 354.11 |
| 10000 | hnsw_40 | 0.007 | 49.20 | 352.91 |
| 10000 | hnsw_100 | 0.007 | 51.40 | 354.45 |
| 10000 | hnsw_200 | 0.007 | 49.66 | 354.06 |
| 10000 | hybrid_10 | 0.007 | 257.54 | 352.98 |

## Accuracy under scale

ANN recall@10 compares chunk IDs against PostgreSQL exact search over the same
cached vectors and query pack. Its HNSW execution path is separately verified;
iterative_scan=strict_order is preserved. Only one corpus is present at a time,
so selective multi-corpus filtering is not tested. Independent HNSW builds may
produce different graphs. All four validated runs recorded zero queries with an
exact 10th/11th score gap below 1e-6.

| N | ef_search | Recall@10 repeat 1 | Recall@10 repeat 2 |
| --- | --- | --- | --- |
| 1000 | 40 | 0.9170 | 0.9170 |
| 1000 | 100 | 0.9725 | 0.9695 |
| 1000 | 200 | 0.9905 | 0.9905 |
| 10000 | 40 | 0.7885 | 0.8040 |
| 10000 | 100 | 0.8925 | 0.8985 |
| 10000 | 200 | 0.9540 | 0.9570 |

### Original-ID retention under copied text

The following strict values use the original six records and original section IDs.
The distractors are made by randomly recombining real sentences: answer sentences
are copied under unlabelled IDs. These values therefore cannot measure retrieval
quality as a corpus grows. Copies can displace the labelled original even when
retrieved text still contains source sentences. Labels and the six original JSON
files remain unchanged.

| Method | Real 179 | Real +1k repeat 1 | Real +1k repeat 2 | Real +10k repeat 1 | Real +10k repeat 2 |
| --- | --- | --- | --- | --- | --- |
| memory_bm25 | 0.7917 | 0.5938 | 0.5938 | 0.2240 | 0.2240 |
| memory_vector | 0.7188 | 0.5156 | 0.5156 | 0.2969 | 0.2969 |
| postgres_fts | 0.6406 | 0.6042 | 0.6042 | 0.4635 | 0.4635 |
| postgres_exact | 0.7188 | 0.5156 | 0.5156 | 0.2969 | 0.2969 |
| hnsw_40 | 0.7188 | 0.5156 | 0.5156 | 0.2344 | 0.2344 |
| hnsw_100 | 0.7188 | 0.5156 | 0.5156 | 0.2500 | 0.2500 |
| hnsw_200 | 0.7188 | 0.5156 | 0.5156 | 0.2500 | 0.2500 |
| hybrid_10 | 0.7135 | 0.6198 | 0.6198 | 0.4375 | 0.4375 |

### Copied-sentence-adjusted attribution

[Post-hoc attribution record](../perf/results/copied-text-attribution.json).
This untimed pass rebuilds rankings once for each population/method, then scores
strict and adjusted on exactly the same ranking. The historical six records lack
ranked chunk IDs, so this is a separate pass, not a relabeling of those files.
New HNSW index builds may differ from the historical graphs.

A synthetic hit is credited to a relevant original section when its body contains
one complete source sentence/line unit of at least six whitespace-delimited words.
Units use the generator's punctuation-plus-whitespace or newline splitter. Matching
normalizes whitespace only and preserves token boundaries, case and punctuation.
Only synthetic passages receive aliases; real-section labels stay unchanged.

Both metrics use the first five distinct retrieved section IDs from search(k=30).
Attribution never reranks, collapses copied aliases, or pulls additional hits into
those five positions. Recall's denominator remains the original relevant-section
count. A section is credited at most once; one copied hit may credit several
relevant sections. This is deliberately permissive attribution: any qualifying
sentence counts, whether or not it states a question's required key fact. It is
not an answer-correctness score.

| Method | Real 179 strict = adjusted | +1k strict | +1k adjusted | +10k strict | +10k adjusted |
| --- | --- | --- | --- | --- | --- |
| memory_bm25 | 0.7917 | 0.5938 | 0.8229 | 0.2240 | 0.7344 |
| memory_vector | 0.7188 | 0.5156 | 0.7656 | 0.2969 | 0.6979 |
| postgres_fts | 0.6406 | 0.6042 | 0.7604 | 0.4635 | 0.8125 |
| postgres_exact | 0.7188 | 0.5156 | 0.7656 | 0.2969 | 0.6979 |
| hnsw_40 | 0.7188 | 0.5156 | 0.7656 | 0.2344 | 0.7031 |
| hnsw_100 | 0.7188 | 0.5156 | 0.7656 | 0.2500 | 0.7083 |
| hnsw_200 | 0.7188 | 0.5156 | 0.7656 | 0.2500 | 0.7135 |
| hybrid_10 | 0.7135 | 0.6198 | 0.7917 | 0.4375 | 0.7969 |

Hybrid's adjusted attribution is 0.7135 on the real corpus,
0.7917 with 1k copies and 0.7969 with 10k copies.
Its strict original-ID retention is 0.6198 and 0.4375 in those copied populations.
The apparent collapse in original-ID retention therefore cannot establish a
retrieval-quality collapse. The adjusted proxy also cannot establish improved
answer quality or generalize to independently written documents.

Duplicate content occupies ranking positions; this motivates a later deduplication
experiment. No deduplication, ranking change, label change or default change is
implemented here. ANN neighbor recall above is a separate vector-index metric.

## Profiles, execution plans and bottlenecks

The three slowest stage types at the largest measured scale (cold encoding plus
validated repeat 1) are:

| Stage type | Worker wall s | Scope |
| --- | --- | --- |
| Cold encoding | 300.04 | 10k cold preparation including model/warmup/token/profile work |
| ANN/dev/profile phase | 42.67 | 200 paired ANN queries plus both dev populations |
| Hybrid timing worker | 12.92 | 200 searches plus setup/warmup/memory probe |

Full top-15 cProfile tables are in measurements.encoding.batch_profile,
queries.memory_bm25.profile and quality.context_profile. One 16-passage batch and
the first seeded query are profiled. Context ranking is prefetched: selection
excludes retrieval and ContextRetriever construction. Verbatim excerpts:

```text
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    0.427    0.427 embeddings.py:67(_encode)
        1    0.427    0.427    0.427    0.427 onnxruntime_inference_collection.py:372(run)
        1    0.001    0.001    0.001    0.001 {method 'encode_batch' of 'tokenizers.Tokenizer' objects}
```

```text
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.001    0.001    0.014    0.014 retrieval.py:88(search)
    10000    0.004    0.000    0.011    0.000 retrieval.py:77(score)
    40000    0.007    0.000    0.007    0.000 {method 'get' of 'dict' objects}
        1    0.002    0.002    0.002    0.002 {method 'sort' of 'list' objects}
```

```text
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    0.000    0.000 context.py:40(context)
        5    0.000    0.000    0.000    0.000 __init__.py:183(dumps)
        5    0.000    0.000    0.000    0.000 dataclasses.py:1308(asdict)
```

Inference dominates the batch. BM25 visits all 10k chunks (40k dictionary lookups
for this four-term query) and then sorts. The isolated context sample is below
the profiler's displayed millisecond precision; it is not an evidenced latency
bottleneck.

Largest-scale raw EXPLAIN (ANALYZE, BUFFERS) plans are in
queries.postgres_fts.explain and queries.hnsw_100.explain; forced-ANN plans are
under quality.ann_explain. This projection preserves selected raw fields and
the plan tree. Full costs, filters, planning times and buffers remain in JSON.

```json
{
  "postgres_fts": {
    "Execution Time": 28.896,
    "Plan": {
      "Node Type": "Limit",
      "Actual Rows": 10.0,
      "Shared Hit Blocks": 39788,
      "Shared Read Blocks": 0,
      "Plans": [
        {
          "Node Type": "Sort",
          "Actual Rows": 10.0,
          "Shared Hit Blocks": 39788,
          "Shared Read Blocks": 0,
          "Plans": [
            {
              "Node Type": "Seq Scan",
              "Actual Rows": 5490.0,
              "Rows Removed by Filter": 4510,
              "Shared Hit Blocks": 39782,
              "Shared Read Blocks": 0
            }
          ]
        }
      ]
    }
  },
  "hnsw_100": {
    "Execution Time": 1.778,
    "Plan": {
      "Node Type": "Limit",
      "Actual Rows": 10.0,
      "Shared Hit Blocks": 2050,
      "Shared Read Blocks": 0,
      "Plans": [
        {
          "Node Type": "Index Scan",
          "Index Name": "chunks_embedding_idx",
          "Actual Rows": 10.0,
          "Rows Removed by Filter": 0,
          "Shared Hit Blocks": 2050,
          "Shared Read Blocks": 0
        }
      ]
    }
  }
}
```

FTS chooses a sequential scan despite GIN availability: the OR-expanded query
matches much of the sentence-mixed corpus and ranking touches many buffers.
HNSW is used at 10k, not 1k. Server execution time is not end-to-end latency:
encoding, new connection setup, Docker VM transport and Python work remain outside
EXPLAIN. [pgvector planning and recall guidance](https://github.com/pgvector/pgvector).

Optimization hypotheses only; none is implemented:

- Length-bucketed BGE batches might reduce padding and native memory. The profile
  establishes inference cost, not a measured padding-speedup claim.
- BM25 inverted postings, cached normalization and bounded top-k might reduce
  full-scan work; score/order parity and memory require measurement.
- Connection reuse might reduce fixed PostgreSQL overhead. More selective FTS
  workloads might benefit from GIN; neither automatically removes broad-term
  ranking cost. Compare plans and dev quality before changing queries.
- Larger ef_search might recover ANN neighbors at higher traversal cost.
  Synthetic recall on a laptop does not select a production setting.
- Token accounting belongs in a separate correctness/quality experiment. The real
  truncated passages justify investigation; this report does not change chunking,
  context selection, byte budgets or model behavior.

## Reproduce and verify

Run from assistant/. Recorded commands assume pinned weights already exist in the
Hugging Face cache; remove HF_HUB_OFFLINE=1 for the first weight download. No paid
inference credential is needed. Docker must be running.

```sh
UV_CACHE_DIR=.cache/uv uv sync --locked
for perf_scale in 1000 10000; do
  for perf_iteration in 1 2; do
    HF_HUB_OFFLINE=1 .venv/bin/python scripts/benchmark_retrieval.py --scale "$perf_scale" --seed 20261111 --output ".cache/perf/latest-$perf_scale.json"
    cp ".cache/perf/latest-$perf_scale.json" "perf/results/$perf_scale-run$perf_iteration.json"
  done
done
```

For a new cold-vector measurement, pass --cache .cache/perf-fresh. Later invocations
reuse that cache and do not report cached loading as encoding throughput.
Run invocations sequentially. The unexecuted 100k command is:

```sh
HF_HUB_OFFLINE=1 .venv/bin/python scripts/benchmark_retrieval.py --scale 100000 --allow-large --seed 20261111 --output .cache/perf/latest-100000.json
```

Existing CI discovery runs two N=200 HashingEmbedder smoke tests: in-memory and
disposable PostgreSQL. Stand-in output cannot be marked as quality evidence.
Negative tests cover bad percentiles, deterministic/different seeds, missing
environment fields, cache corruption/identity, wrong ANN plans and 100k opt-in.
No workflow or dependency change is needed.

Attribution follow-up checks: Ruff check/format pass; 208 default tests pass; 56 PostgreSQL/model
tests pass with the existing one strict xfail for v1-011. The unchanged dev
evaluation gate passed locally with its committed baseline in the original P1 run.
That original validation also passed core make check and make test (1,725 tests),
React build and 3 tests, 3 scenario snapshots, and 2 fake agent/handoff tests.
The existing gate and these suites run again in PR CI; this benchmark does not
replace a gate.

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -m "postgres or model"
.venv/bin/python scripts/benchmark_retrieval.py --scale 200 --smoke --memory-only --output .cache/perf/smoke.json
.venv/bin/python scripts/benchmark_retrieval.py --scale 200 --smoke --output .cache/perf/pg-smoke.json
```

Break it: remove an environment field or mark hashing as a quality signal;
validate_result must refuse to write it. Substitute a primary-key bitmap plan for
ANN; require_hnsw_plan must reject it. Omit --allow-large at 100000; the experiment
must stop before starting.

Recompute repeat deltas directly from committed samples:

```python
import json
from pathlib import Path
from ledgerops_assistant.benchmark import percentiles, validate_result

for scale in (1000, 10000):
    first, second = [
        json.loads(Path(f"perf/results/{scale}-run{i}.json").read_text()) for i in (1, 2)
    ]
    for record in (first, second):
        validate_result(record)
    for method in first["queries"]:
        a, b = [percentiles(r["queries"][method]["samples_ms"])["p50"] for r in (first, second)]
        print(scale, method, a, b, 100 * (b / a - 1))
```

These records concern local retrieval, resources and source retention. They do
not establish answer quality, injection resistance, real operator traffic,
cold-query latency, concurrency capacity or 100k behavior.

## Reproduce copied-text attribution

This is a separate, untimed attribution pass over the 32 development questions.
It reuses the original cached corpora/vectors, verifies their digests and checksums,
and checks that the six input records are byte-identical before and after the pass.
It does not call the benchmark CLI, retime searches, re-encode passages or repeat
profiles. No v2/v3 questions or answer model are used.

Run from assistant/ after the original caches exist. The following block is the
entire post-processing procedure; the unit tests load this same published code.

```python
# copied-text-attribution-v1
import hashlib
import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import psycopg

from ledgerops_assistant import benchmark as bench
from ledgerops_assistant.corpus import Corpus, load_corpus
from ledgerops_assistant.postgres import index_corpus, setup


def sentence_evidence(source, relevant_sections):
    evidence = {section: {} for section in sorted(relevant_sections)}
    for chunk in source.chunks:
        if chunk.section_id not in evidence:
            continue
        for unit in re.split(r"(?<=[.!?])\s+|\n+", chunk.body):
            quote = " ".join(unit.split())
            if len(quote.split()) >= 6:
                evidence[chunk.section_id].setdefault(quote, chunk.chunk_id)
    return evidence


def copied_text_scores(hits, relevant_sections, evidence):
    relevant = set(relevant_sections)
    unique = {}
    for hit in hits:
        unique.setdefault(hit.chunk.section_id, hit.chunk)
    selected = list(unique.values())[:5]
    strict = {chunk.section_id for chunk in selected} & relevant
    adjusted = set(strict)
    rows = []
    for chunk in selected:
        copies = []
        if chunk.path.startswith("__benchmark__/"):
            body = " " + " ".join(chunk.body.split()) + " "
            for section in sorted(relevant):
                for quote, origin in evidence[section].items():
                    if " " + quote + " " in body:
                        adjusted.add(section)
                        copies.append(
                            {"section_id": section, "source_chunk_id": origin, "quote": quote}
                        )
                        break
        rows.append(
            {
                "section_id": chunk.section_id,
                "chunk_id": chunk.chunk_id,
                "copied_sentence_evidence": copies,
            }
        )
    return {
        "strict": len(strict) / len(relevant),
        "adjusted": len(adjusted) / len(relevant),
        "strict_sections": sorted(strict),
        "adjusted_sections": sorted(adjusted),
        "top_five": rows,
    }


def cached_matrix(path, corpus, name):
    path = Path(path)
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["sha256"] == bench.digest_file(path)
    assert meta["corpus_digest"] == corpus.digest and meta["embedder"] == name
    matrix = np.load(path)
    assert matrix.shape == (len(corpus.chunks), 384)
    return matrix, meta["sha256"]


def main():
    source = load_corpus()
    questions = bench._dev_questions(source.root)
    evidence = sentence_evidence(
        source, {section for q in questions for section in q["relevant_sections"]}
    )
    originals = {
        f"perf/results/{n}-{suffix}.json": bench.digest_file(
            Path(f"perf/results/{n}-{suffix}.json")
        )
        for n in (1000, 10000)
        for suffix in ("cold-encoding", "run1", "run2")
    }
    recorded = json.loads(Path("perf/results/1000-run1.json").read_text())
    assert source.digest == recorded["source"]["digest"]
    assert all(
        bench.digest_file(Path(name)) == digest for name, digest in recorded["code_sha256"].items()
    )
    assert (
        bench.digest_file(Path("eval/v1-draft/questions.jsonl"))
        == (recorded["quality"]["dev_file_sha256"])
    )
    report = Path("docs/PERFORMANCE.md").read_text()
    marker = chr(96) * 3 + "python\n# copied-text-attribution-v1\n"
    analysis = (
        "# copied-text-attribution-v1\n" + report.split(marker, 1)[1].split(chr(96) * 3, 1)[0]
    )
    encoder = bench._encoder("bge")
    info = json.loads(
        Path(".cache/perf/sentence-mix-v1-1000-20261111-bge/vectors.json").read_text()
    )
    real, real_sha = cached_matrix(info["real"], source, encoder.name)
    result = {
        "schema_version": 1,
        "scope": "post_hoc_copied_sentence_attribution",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "analysis_sha256": hashlib.sha256(analysis.encode()).hexdigest(),
        "source_corpus_digest": source.digest,
        "dev_questions_sha256": bench.digest_file(Path("eval/v1-draft/questions.jsonl")),
        "original_six_records_sha256": originals,
        "embedder": encoder.name,
        "vector_cache_sha256": {"real": real_sha},
        "definition": {
            "sentence_units": "generator punctuation-plus-whitespace or newline splitter",
            "minimum_whitespace_words": 6,
            "normalization": "whitespace only",
            "match": "complete unit with token boundaries; synthetic passages only",
            "ranking": "top 5 unique retrieved section IDs from search(k=30)",
            "denominator": "number of original relevant sections",
            "rerank_or_deduplicate_after_attribution": False,
            "strict_and_adjusted_use_same_hits": True,
        },
        "new_non_timed_ranking_pass": True,
        "original_benchmark_rerun": False,
        "independent_hnsw_index_builds": True,
        "populations": {},
    }
    with bench.disposable_postgres() as dsn:
        setup(dsn)
        result["environment"] = bench.environment(dsn, True)
        for scale in (0, 1000, 10000):
            data, matrix = source, real
            if scale:
                cache = Path(f".cache/perf/sentence-mix-v1-{scale}-20261111-bge")
                synthetic = bench._load_corpus(cache / "synthetic.jsonl", source.root)
                old = json.loads(Path(f"perf/results/{scale}-run1.json").read_text())
                assert synthetic.digest == old["measurements"]["generation_and_chunking"]["digest"]
                info = json.loads((cache / "vectors.json").read_text())
                vectors, sha = cached_matrix(info["synthetic"], synthetic, encoder.name)
                result["vector_cache_sha256"][str(scale)] = sha
                data = Corpus(
                    source.root, "copied-text-attribution", source.chunks + synthetic.chunks, {}
                )
                matrix = np.concatenate([real, vectors])
            bench._reset_owned_database(dsn)
            index_corpus(dsn, data, bench.CachedPassages(encoder.name, matrix))
            with psycopg.connect(dsn) as conn:
                conn.execute("ANALYZE assistant_rag.chunks")
            population = {"chunks": len(data.chunks), "digest": data.digest, "methods": {}}
            for name in bench.METHODS:
                retriever = bench._retriever(name, data, encoder, matrix, dsn)
                rows = {
                    q["id"]: copied_text_scores(
                        retriever.search(q["question"], 30), q["relevant_sections"], evidence
                    )
                    for q in questions
                }
                population["methods"][name] = {
                    metric: statistics.mean(row[metric] for row in rows.values())
                    for metric in ("strict", "adjusted")
                }
                population["methods"][name]["per_question"] = rows
                print(
                    scale,
                    name,
                    {k: population["methods"][name][k] for k in ("strict", "adjusted")},
                    flush=True,
                )
            result["populations"][str(scale)] = population
    assert all(bench.digest_file(Path(path)) == sha for path, sha in originals.items())
    bench.write_json(Path("perf/results/copied-text-attribution.json"), result)


if __name__ == "__main__":
    main()
```

Execute only that marked block:

```sh
HF_HUB_OFFLINE=1 .venv/bin/python - <<'PY'
from pathlib import Path
report = Path("docs/PERFORMANCE.md").read_text()
marker = chr(96) * 3 + "python\n# copied-text-attribution-v1\n"
source = "# copied-text-attribution-v1\n" + report.split(marker, 1)[1].split(chr(96) * 3, 1)[0]
exec(compile(source, "docs/PERFORMANCE.md", "exec"), {"__name__": "__main__"})
PY
```
