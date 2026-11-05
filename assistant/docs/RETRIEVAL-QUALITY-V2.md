# Retrieval quality: diagnosis, development selection and new holdout

## What changed

The production hybrid retains the same local bge-small encoder, corpus, chunk IDs,
PostgreSQL FTS, HNSW settings, candidate depth 50 and equal component weights.
Only the RRF constant changes from 60 to **10**. Twenty-four configurations were
compared on the existing 32 development retrieval questions. Selection maximized
Recall@5 while requiring MRR@10 and nDCG@5 to meet the old hybrid development
baseline. Ties used nDCG, then MRR, then canonical configuration JSON.

The grid tested keyword weights 0.25/0.5/1.0, constants 10/60, depths 20/50 and
chunk/section voting. Weighted or section-level fusion did not win under that
predeclared rule. They remain explicit experiment options, not silent production
changes. Every trial was recorded in local MLflow with its configuration.

Development Recall@5 changes from **0.7083 to 0.7812**, MRR from 0.7527 to 0.7531,
and nDCG from 0.6741 to 0.7048. These are selection scores on previously tuned,
optimistic development data, not confirmatory evidence.

## Why equal fusion lost some vector results

At k=60 and depth=50, any chunk found by both components scores at least
`2/(60+50)=0.01818`. A chunk present in only one list scores at most
`1/(60+1)=0.01639`. Every overlap candidate therefore outranks every single-list
candidate, even vector rank 1. Wide lexical OR matching can turn weak common-word
agreement into a hard preference over strong semantic evidence. At k=10, deep
agreement no longer necessarily overwhelms a single high-ranked result.

The original [RRF paper](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
selected 60 on its own pilot data; it is not a universal constant. PostgreSQL
`ts_rank_cd(...,32)` uses cover density and a monotonic score normalization, not
corpus-wide BM25 IDF. Flag 32 alone cannot change rank order.
[PostgreSQL ranking documentation](https://www.postgresql.org/docs/current/textsearch-controls.html#TEXTSEARCH-RANKING)
describes these distinctions.

Raw development traces identify these concrete cases:

| Question | Component ranks of relevant chunk | Old hybrid | k=10 |
|---|---|---|---|
| v1-011 | vector 4, FTS 37 | chunk 8 / section 6 | chunk 7 / section 5 |
| v1-014 | vector 2, FTS 18 | chunk 6 | chunk 4 |
| v1-023 | vector 4, absent FTS top 50 | chunk 29 / section 22 | chunk 15 / section 12 |
| v1-027 | vector 2, FTS 16 | chunk 7 | chunk 5 |

The corpus has 175 chunks and 105 sections; 42 sections have multiple chunks.
Fusion votes on chunk IDs, while the historical metric deduplicates sections
after fetching 30 chunks. Repeated chunks can consume ranks, and complementary
chunks from one section do not count as shared chunk-level evidence.

That evaluation also differs from the runtime's five supplied chunks. Development
coverage in the actual five chunks is vector 0.7188, old hybrid 0.6771 and k=10
0.7031. Thus the higher section Recall does not establish better runtime coverage
than vectors. The new `context_recall@5` reports this source-section coverage
explicitly; it still does not prove the retrieved passage contains every fact.

Some old labels omit directly supporting alternative sections. For v1-023, the
unlabelled reconciliation publication section explicitly prohibits writes to
balances, entries and holds. For v1-011, a hit anywhere in the labelled section
earns credit even when the required release rule is in another chunk. Old labels
and results were preserved rather than revised to make an approach win.

HNSW matches exact vector retrieval metrics, including per-question scores, on
the development evidence and the new holdout. This does not prove identical full
rankings; approximate-index error is not supported as the cause here. Three long
chunks exceed the encoder's token window, but that is a separate limitation.
The exposed original 12-case holdout is diagnostic history and was not used for
this selection.

## New held-out comparison

The new set has **60 answerable questions, 29 topic groups and 20 source documents**:
44 reasoning/paraphrase questions and 16 precise code/command questions. A separate
author read only the indexed documentation and corpus-ID rules, without old
questions, rankings, retrieval implementation or candidate settings.

Another agent checked all labels and 137 exact evidence quotes. Its semantic
review completed before an agent-inventory response exposed old-development
summaries; subsequent edits were limited to previously identified whitespace and
one question ambiguity correction. No new-holdout scores existed then. This
exposure is disclosed in the manifest. Neither agent review is human review.
There are no exact normalized question duplicates against either older set;
semantic overlap of documented facts cannot be excluded.

Commit `1f0e7ca` freezes the chosen configuration, new question hash, corpus/model
identity and hashes of 12 relevant implementation/dependency files before the
first validation. The optimizer did not inspect new question/label content before
that freeze. A canonical exclusive attempt record prevents silently overwriting or
rerunning validation under another output name. No parameter, label or ranking
code was changed after seeing the new scores.

| Method | Recall@5 | MRR@10 | nDCG@5 | Actual 5-chunk section coverage |
|---|---:|---:|---:|---:|
| PostgreSQL FTS | 0.8639 | 0.6938 | 0.7049 | 0.8222 |
| HNSW vectors | 0.8361 | 0.7733 | 0.7529 | 0.8278 |
| Exact vectors | 0.8361 | 0.7733 | 0.7529 | 0.8278 |
| Old hybrid, k=60 | 0.8639 | 0.8335 | 0.8073 | 0.8472 |
| Selected hybrid, k=10 | **0.8889** | 0.8277 | **0.8158** | **0.8722** |

Mean retrieval latency was 21.0 ms for selected hybrid and 21.3 ms for old hybrid
on this local run, compared with 12.6 ms for vectors. These are retrieval timings,
not end-to-end service latency or scale measurements.

## Uncertainty and failures

Paired topic-cluster bootstrap resamples the 29 groups, retaining all questions
from each sampled group. These nominal 95% intervals are primary; question-level
bootstrap is also preserved. Neither supports broad production claims.

| Change | Metric | Mean difference | Topic-cluster 95% interval |
|---|---|---:|---|
| Old hybrid → selected | Recall@5 | +0.0250 | [0.0000, +0.0656] |
| Old hybrid → selected | MRR@10 | -0.0058 | [-0.0193, +0.0025] |
| Old hybrid → selected | nDCG@5 | +0.0085 | [-0.0040, +0.0253] |
| Vectors → selected | Recall@5 | +0.0528 | [+0.0164, +0.0968] |
| Vectors → selected | MRR@10 | +0.0543 | [+0.0010, +0.1097] |
| Vectors → selected | nDCG@5 | +0.0629 | [+0.0238, +0.1013] |
| Vectors → selected | Actual 5-chunk coverage | +0.0444 | [-0.0025, +0.0994] |

Against the old hybrid, Recall improves on only blind-v2-003 and -010 and does
not fall on any question. MRR falls on -012, -014 and -040. Against vectors,
Recall falls on -021; actual context coverage also falls on -025. All per-question
scores and regression IDs are preserved. Runtime coverage and superiority over
old hybrid remain uncertain; MRR is a measured tradeoff.

Absolute scores on this new set must not be compared with 0.3333 on the old
12-question holdout as if the difference measured improvement. The questions,
coverage and labels differ. Only same-question comparisons above are meaningful.
This remains a synthetic closed-document benchmark. It does not measure real
operator queries, real-model prose, semantic citation entailment or injection safety.
No paid or subscription inference was used.

## Evidence and reproduction

- [Frozen protocol, grid and selection](../eval/v2-study/selection.json)
- [Component-rank diagnosis](../eval/v2-study/diagnosis.json)
- [Frozen code/configuration](../eval/v2-study/freeze.json)
- [Questions and source annotations](../eval/v2-holdout/questions.jsonl)
- [Author/audit disclosure](../eval/v2-holdout/manifest.json)
- [Compact held-out report](../eval/results/v2-postgres-holdout.json)
- [Per-question scores](../eval/results/v2-postgres-holdout.csv)

Full ranking/evaluation artifacts remain in local MLflow; the public repository
contains compact summaries and scores rather than a second full answer dump.
Held-out MLflow run: `6347aab5d58246388dc43119a07b8ff4`.

From the repository root, with Docker available:

```sh
uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --split dev --baseline assistant/eval/results/v2-postgres-dev.json \
  --output assistant/.cache/reproduced-dev.json
```

To reproduce the old baseline, add `--fusion legacy` and compare against the
historical v1 report. To reproduce the grid:

```sh
mkdir -p assistant/.cache/study-v2
cp assistant/eval/v2-study/protocol.json assistant/.cache/study-v2/protocol.json
uv run --project assistant --locked python assistant/scripts/retrieval_study.py tune
```

The published attempt record is deliberately consumed. For an explicitly labelled
reproduction (not another unseen validation), copy `freeze.json` into a fresh local
directory and run the following from the same frozen source/dependency revision:

```sh
uv run --project assistant --locked python assistant/scripts/retrieval_study.py validate \
  --questions assistant/eval/v2-holdout/questions.jsonl \
  --freeze /absolute/reproduction/freeze.json \
  --output /absolute/reproduction/report.json
```

CI uses only the existing development questions. It now rejects decreases in
Recall, MRR, nDCG and actual five-chunk section coverage, and rejects changes to
label/model identity, corpus digest or fusion configuration. The newly exposed
holdout is not a CI tuning target. Future optimization needs another unseen set.
