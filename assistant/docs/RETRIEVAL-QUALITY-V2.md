# Retrieval quality: diagnosis, development selection and new holdout

The v2 study and #19 context measurements below are historical. Current CI uses
the reviewed main-document baseline described in
[Current main document integration](#current-main-document-integration).

## What changed

In the historical v2 study, the hybrid retained the same local bge-small encoder, corpus, chunk IDs,
PostgreSQL FTS, HNSW settings, candidate depth 50 and equal component weights.
Only the RRF constant changes from 60 to **10**. Twenty-four configurations were
compared on the existing 32 development retrieval questions. Selection maximized
Recall@5 while requiring MRR@10 and nDCG@5 to meet the old hybrid development
baseline. Ties used nDCG, then MRR, then canonical configuration JSON.

The grid tested keyword weights 0.25/0.5/1.0, constants 10/60, depths 20/50 and
chunk/section voting. Weighted or section-level fusion did not win under that
predeclared rule. They remain explicit experiment options, not silent production
changes. Every trial was recorded in local MLflow with its configuration. The published
`selection.json` predates the `context_recall@5` metric, so its grid omits that
column. Selection used only Recall, MRR and nDCG; the missing column did not change
the selected configuration.

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
44 reasoning/paraphrase questions and 16 precise code/command questions. The
questions and documentation-grounded labels are synthetic and have not been
independently human-reviewed. Source validation checked 137 exact evidence quotes
before exposure to earlier development metrics and protocol critiques. Later edits
were limited to previously identified quote corrections, one disambiguation and
metadata; new-holdout scores were unavailable. The manifest retains that limitation.
There are no exact normalized question duplicates against either older set;
semantic overlap of documented facts cannot be excluded.

Commit `1f0e7ca` freezes the chosen configuration, new question hash, corpus/model
identity and hashes of 12 relevant implementation/dependency files before the
first validation. Configuration selection used only development questions before
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

## Distribution and scope of the result

The new holdout differs substantially from the development questions: its questions
average approximately **26 words**, versus **11** in development. Approximately
**33%** concern operational documents, versus **97%** in development. The remainder
covers the wider indexed documentation, including engineering and protocol rules.

The comparison therefore concerns a broader general-document question distribution.
At most it supports a limited non-regression conclusion for retrieval coverage on
that benchmark, with the MRR tradeoff below. It does **not** demonstrate better
answers to short operator questions. The set contains only answerable retrieval
questions; it does not validate refusals, live investigation or prompt-injection
resistance. The distribution shift is also why absolute scores from the older
holdout cannot measure an improvement.

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
This retrieval study used no paid or subscription inference.

## Evidence and reproduction

- [Frozen protocol, grid and selection](../eval/v2-study/selection.json)
- [Component-rank diagnosis](../eval/v2-study/diagnosis.json)
- [Frozen code/configuration](../eval/v2-study/freeze.json)
- [Questions and source annotations](../eval/v2-holdout/questions.jsonl)
- [Dataset provenance and limitations](../eval/v2-holdout/manifest.json)
- [Compact held-out report](../eval/results/v2-postgres-holdout.json)
- [Per-question scores](../eval/results/v2-postgres-holdout.csv)

Full ranking/evaluation artifacts remain in local MLflow; the public repository
contains compact summaries and scores rather than a second full answer dump.
Held-out MLflow run: `6347aab5d58246388dc43119a07b8ff4`.

From the historical study checkout (use the frozen revision above), with Docker available:

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
directory and run the following from the same frozen source/dependency revision
(`1f0e7ca`, also unchanged through PR #17). Subsequent metadata-loader cleanup
changes the historical evaluation file hash without changing ranking/scoring; it
does not rewrite the original freeze or claim that the new commit has its hash:

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


## Retrieval storage and local experiments

The assistant owns a dedicated database, separate from the financial ledger.
`001_rag.sql` publishes immutable corpus snapshots with 384-dimensional vectors,
a cosine HNSW index (`m=16`, `ef_construction=64`) and an English tsvector/GIN
index. The snapshot identity binds corpus digest and pinned embedding identity;
embedding runs outside the transaction, then a complete snapshot publishes
atomically. Concurrent indexers cannot publish half a corpus.

Keyword retrieval uses `websearch_to_tsquery` with OR between normalized terms and
`ts_rank_cd`: PostgreSQL FTS, not BM25. HNSW uses `ef_search=100` and strict iterative
scans for corpus filtering. An exact path disables index scans in its own read-only
transaction. Small-corpus queries may still choose sequential scans, so the
integration suite separately checks an HNSW execution plan. Equal RRF component
weights combine ranks instead of incompatible raw scores.

The migration owner may install pgvector. The runtime has USAGE/SELECT on RAG
schemas plus CRUD on its dedicated workflow/checkpoint records, without financial
schema access. Never give this package a ledger database credential. Set
`LEDGEROPS_ASSISTANT_DATABASE_URL` for a persistent dedicated DB and
`LEDGEROPS_ASSISTANT_CORPUS_ROOT` for an installed package's document root.

Without a supplied DSN, `scripts/evaluate_local.py` creates an isolated disposable
Docker database with a temporary password and removes only that container. All
experiments write local MLflow SQLite/artifacts under `.cache/mlflow`; no tracking
service receives them. Read records with `MlflowClient`, or point a separately
installed MLflow UI at that database. The existing development-only command above
reproduces the CI quality gate with pinned public local embeddings and fake answers.

## Historical 60-question comparison

The earlier synthetic set remains in `eval/v1-draft/questions.jsonl`, with 40
development and 20 held-out questions. Of these, 32 and 12 respectively were scored
for retrieval; remaining cases test routing/refusal. The current public manifest
records four later development relevance additions and the preceding file hash.
Question wording, key facts, routes and the 20 test records remain unchanged;
labels are not independently human-reviewed. Use 22c56c7 for the original question
bytes. The original k=60 development and held-out runs remain historical comparisons.

| Split | Method | Recall@5 | MRR@10 | nDCG@5 |
|---|---|---:|---:|---:|
| Development | PostgreSQL FTS | 0.6250 | 0.6084 | 0.5520 |
| Development | HNSW/exact vectors | 0.7344 | 0.6687 | 0.6253 |
| Development | Old hybrid, k=60 | 0.7083 | 0.7527 | 0.6741 |
| Held out | PostgreSQL FTS | 0.4167 | 0.1944 | 0.2494 |
| Held out | HNSW/exact vectors | 0.5000 | 0.3750 | 0.4129 |
| Held out | Old hybrid, k=60 | 0.3333 | 0.2989 | 0.2718 |

The historical held-out generation used a fake sentence copier: route accuracy was
17/20, correct refusals 5/8 and relevant-section citation hits 2/12. These expose
failures; they do not demonstrate factual accuracy or injection resistance. The
sample is too small for broad rankings. HNSW/exact agreement on 175 chunks is not
an approximate-search benchmark at scale. Raw reports retain corpus/model/label
identities and MLflow IDs:
[v1 development](../eval/results/v1-postgres-dev.json),
[v1 held out](../eval/results/v1-postgres-holdout.json).

The still-supported in-memory CLI is a prototype: its keyword method is BM25 and
its unchanged scope heuristic came from an older synthetic calibration set. Those
obsolete questions/results are archived rather than presented as current evidence.
The PostgreSQL service and its quality gate use the preserved development set and
the configuration described above.

References: [pgvector](https://github.com/pgvector/pgvector),
[PostgreSQL text search](https://www.postgresql.org/docs/current/textsearch-controls.html),
[MLflow tracking](https://mlflow.org/docs/latest/ml/tracking/).


## Bounded generation context (development only)

This subsection records #19 at 22c56c7, using the 175-chunk corpus and original
labels. Reproduce its tables and commands from that revision. Current-main
development results are reported separately below.

A section hit can miss the paragraph needed to answer. Inspection of the actual
PostgreSQL inputs found two distinct cases:

- v1-011's raw five chunks contain three chunks from one maintenance section but
  no authenticated-observation section. The section's first appearance among the
  first five **distinct sections from 30 ranks** is not the same as supplying it in
  the first five chunks.
- v1-019 receives the one-chunk "Read the report" section. Its HELD rule is in the
  adjacent **Time and scope heading**, not a later paragraph in the same section.

Generation now uses policy `section-neighbors-v1`. Raw `search()` rankings are
unchanged. From 30 candidates, select the first-ranked chunk of each of five distinct
sections, retain those anchors, add each selected section's introductory chunk,
then consider forward/backward neighbors at distances 1 and 2 in document order.
Never cross a document boundary; only expand anchors matching the loaded immutable
corpus snapshot. Deduplicate by chunk ID. Every supplied chunk retains its complete
original text, heading and citation ID.

The pack fits 18,000 bytes of serialized chunk JSON, including metadata. Whole
chunks that do not fit are omitted; text is not cut under an unchanged citation
ID. The agent additionally removes trailing document chunks when live API
observations need space within its existing overall input bound. It preserves API
observations, refuses if those alone exceed the bound, and validates citations
against the passages actually sent.

These are development measurements over 32 documentation questions:

| Provided context | Raw five chunks | Expanded |
|---|---:|---:|
| Mean chunks | 5.00 | 11.28 |
| Mean serialized bytes | 8369 | 17720 |
| Largest serialized bytes | 9952 | 18000 |
| Mean labeled-section coverage | 0.7031 | 0.8594 |

An ablation separates section diversity from neighboring source chunks at similar
mean bytes for the first two rows:

| Provided context | Mean bytes | Section coverage |
|---|---:|---:|
| Raw five chunks | 8369 | 0.7031 |
| Five distinct-section anchors only | 8306 | 0.7812 |
| Anchors + introductions + neighbors | 17720 | 0.8594 |

About half the coverage gain comes from distinct-section selection, with almost
no byte increase. The other half comes from introductions and neighbors, with
roughly twice the document bytes. Diversity alone supplies the coverage gain for
v1-004/011/015/028; v1-001/008/017/019 require neighboring chunks. Of the three
factually improved answers, v1-017 and v1-019 depend on those neighbors.

All five retrievers' original per-question ranking metrics match #18 exactly.
Historical `context_recall@5` continues to describe the raw five chunks.
Expanded-context section coverage is recorded separately under `context.stats`;
neither metric proves that each required fact is present or the answer is correct.
Regression tests additionally check that the exact FAILED/CLOSED_ABSENT release
rule and the HELD/MISSING_INTERNAL rule reach the answer provider with original
citation IDs, through real PostgreSQL and the pinned local encoder.

Reproduce the two context measurements using fake generation:

```sh
uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --split dev --context raw --output assistant/.cache/context-raw.json
uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --split dev --context expanded --output assistant/.cache/context-expanded.json
```


<details>
<summary>Reproduce the ablation from saved development reports</summary>

The first five `context_chunks` in this recorded expanded run are its five
distinct-section anchors; all five fit the budget. The following calculation
uses those anchors and the unchanged source chunks, without calling a model or
changing retrieval. Generate the two reports with the fake-provider commands
above, then run from the repository root:

```sh
uv run --project assistant --locked python - \
  assistant/.cache/context-raw.json \
  assistant/.cache/context-expanded.json <<'PY'
import hashlib
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path

from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.evaluation import DEFAULT_QUESTIONS

corpus = load_corpus()
chunks = corpus.by_id()
questions = {
    q["id"]: q
    for q in (json.loads(line) for line in DEFAULT_QUESTIONS.read_text().splitlines())
    if q["split"] == "dev" and q["expected_route"] == "docs"
}
raw, expanded = [json.loads(Path(p).read_text()) for p in sys.argv[1:]]
question_sha = hashlib.sha256(DEFAULT_QUESTIONS.read_bytes()).hexdigest()
assert raw["context"]["policy"] == "raw-five-chunks"
assert expanded["context"]["policy"] == "section-neighbors-v1"
for report in (raw, expanded):
    assert report["questions"]["evaluated_split"] == "dev"
    assert report["questions"]["sha256"] == question_sha
    assert report["corpus"]["digest"] == corpus.digest

def measure(report, anchors_only=False):
    rows = {r["id"]: r for r in report["answers"]["answers"]}
    sizes, coverage = [], {}
    for key, q in questions.items():
        ids = rows[key]["context_chunks"]
        if anchors_only:
            ids = ids[:5]
            assert len(ids) == len({chunks[i].section_id for i in ids}) == 5
        provided = [chunks[i] for i in ids]
        sizes.append(len(json.dumps([asdict(c) for c in provided]).encode()))
        relevant = set(q["relevant_sections"])
        coverage[key] = len({c.section_id for c in provided} & relevant) / len(relevant)
    return statistics.mean(sizes), statistics.mean(coverage.values()), coverage

before, anchors, full = measure(raw), measure(expanded, True), measure(expanded)
print("| Provided context | Mean bytes | Section coverage |")
print("|---|---:|---:|")
for name, result in [
    ("Raw five chunks", before),
    ("Five distinct-section anchors only", anchors),
    ("Anchors + introductions + neighbors", full),
]:
    print(f"| {name} | {result[0]:.0f} | {result[1]:.4f} |")
print("Diversity sufficient:", [
    key for key in questions
    if anchors[2][key] > before[2][key] and full[2][key] == anchors[2][key]
])
print("Neighbors required:", [
    key for key in questions if full[2][key] > anchors[2][key]
])
PY
```

</details>

The normal CLI, service and development evaluator use expanded context. The
evaluator's `--context raw` option preserves the previous generation input policy;
it does not change ranking. MLflow records the context policy, budget, source
coverage and size together with the existing corpus/model/question identities.

The policy was developed against known development failures and is not an unseen
quality result. The v2 holdout was already consumed before this change and was not
rerun or used to select expansion settings. In #19, questions, labels, selection.json
and freeze.json were unchanged. Source additions change application code hashes; use
the recorded historical revision for exact v2-study reproduction.


### Real generation and fact review

One subsequent development run used the existing ChatGPT subscription, the pinned
official SDK and the same low-effort/configured-model setting as #18. The context
policy's source hashes were recorded before this run and remained unchanged.
All questions, labels and ranking parameters stayed fixed.

| Pipeline measure | Archived raw context | Expanded context |
|---|---:|---:|
| Route agreement | 39/40 | 40/40 |
| Correct required refusals | 8/8 | 8/8 |
| Wrong documentation refusals | 1 | 0 |
| Answers citing a labeled section | 28/31 | 30/32 |

Refusal results include deterministic routing/gating; citation overlap is not
factual accuracy. The 32 documentation answers were also read against their
existing required facts:

- v1-011 now states release, merchant 80/0/80 and no outgoing journal.
- v1-019 now states that a provider payment with a local HELD state remains
  MISSING_INTERNAL, and asynchronous processing does not hide it.
- v1-017 now includes the evidence/version/membership/restore/expiry invalidators.
- v1-007 still omits the explicit new operation-ID/key requirement. v1-013 still
  omits bounded quarantine: that general observation source is outside this pack,
  while its callback-specific 401/no-quarantine answer matches the supplied source.
- v1-024 omits the 23/25-hour DST detail even though the source is supplied.
  v1-014 refers to two-actor review but leaves propose/approve sequencing implicit.
  Both details were also missing from the #18 answers; neither omission is a
  regression introduced here. Compared with #18, three answers add missing required
  facts, and no answer loses a previously supplied required fact.

These remaining omissions prevent treating citation or route agreement as
complete-answer accuracy. This is a non-blind development audit, not independent
human validation, and no labels were changed to improve the score. Context growth
also costs input capacity: mean document bytes roughly double. No faster or
cheaper subscription-inference claim follows from this experiment.

MLflow runs: raw context `50ca4b8b95dc460abd594d3b619b2eb4`, expanded fake generation
`fc5f6ef20ebc48db829f7245f4d3a46c`, expanded subscription generation
`a660384db52f42ff96826e1c7c9813a7`. Raw generation records are not committed.


## Current main document integration

The assistant stack now includes main d57f04a through a merge commit. Main changed
README and ADR 0004 and added Kubernetes, Azure and history-checking documentation.
The previous glob would index 206 chunks, changing the corpus identity and
invalidating the frozen baseline even without a textual merge conflict.

The corpus is now the explicit 21-file list in
[src/ledgerops_assistant/corpus.py](../src/ledgerops_assistant/corpus.py), preserving
all paths used by #19 and their current content, including changed README and
ADR 0004. These are the operating, investigation and evidence sources accepted by
the study. Deployment guides AZURE.md/KUBERNETES.md, ADR 0005 and rename proposals
are excluded from automatic inclusion. New files require simultaneous source-label
review and a new development baseline. This is a scope policy, not a score-based
file selection: the updated approved files produce 179 chunks and still reduce
Recall relative to the historical 175-chunk corpus.

The [development label change record](../eval/v1-draft/DEV-LABEL-CHANGES-2026-11-05.md)
checks all 32 documentation questions against changed sections and existing
key_facts, before running retrieval. It adds the new ADR 0004 section only to
v1-001/002/004/005. No existing section is removed, and question text, key facts,
routes, groups, splits and all test records are unchanged. The v2 holdout files
and results are untouched and were not rerun.

Retrieval and context settings remain fixed: bge-small, FTS/vector fusion k=10,
depth=50, equal weights, 30 context candidates, five section anchors and the same
18000-byte introduction/neighbor packing. Generation is fake in all three runs:

| Corpus and labels | Chunks | Hybrid Recall@5 | MRR@10 | nDCG@5 | Expanded section coverage |
|---|---:|---:|---:|---:|---:|
| #19 corpus, original labels | 175 | 0.7812 | 0.7531 | 0.7048 | 0.8594 |
| Current approved corpus, original labels | 179 | 0.6927 | 0.7063 | 0.6387 | 0.7552 |
| Current approved corpus, reviewed labels | 179 | 0.7135 | 0.7573 | 0.6739 | 0.7708 |

Relabeling recognizes additional sufficient sources; it does not improve the
retriever or restore the earlier Recall. The current baseline is
[v3-main-docs-dev.json](../eval/results/v3-main-docs-dev.json). The original
[v2-postgres-dev.json](../eval/results/v2-postgres-dev.json) remains unchanged as
historical evidence. Gate checks still reject future corpus/model/label identity
drift and decreases against the reviewed current baseline.

### Known missing source

v1-011 still has zero labeled-section coverage in the expanded current context.
Its 80/0/80 and no-outgoing-journal facts remain in PAYOUTS.md but are not supplied.
The prior end-to-end source-presence test remains as a strict expected failure
for this exact StopIteration; unrelated errors still fail, and an unexpected pass
requires reviewing/removing the marker. This is one recorded quality regression,
not a passed source-coverage check. No retrieval or context setting was changed
to special-case it.

The current baseline is not a new real-model or unseen-quality result. Earlier
subscription answer audits apply only to their historical corpus. An edit to an
approved file such as PAYOUTS.md requires a fresh source/label review and baseline;
adding an unapproved document alone does not change the index.

### Reproduction

From the current integration checkout with Docker available:

```sh
git worktree add --detach ../ledgerops-study-22c 22c56c7
LEDGEROPS_ASSISTANT_CORPUS_ROOT=../ledgerops-study-22c \
  uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --provider fake --split dev \
  --questions ../ledgerops-study-22c/assistant/eval/v1-draft/questions.jsonl \
  --output assistant/.cache/main-docs-old-old.json
uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --provider fake --split dev \
  --questions ../ledgerops-study-22c/assistant/eval/v1-draft/questions.jsonl \
  --output assistant/.cache/main-docs-new-old.json
uv run --project assistant --locked python assistant/scripts/evaluate_local.py \
  --provider fake --split dev --baseline assistant/eval/results/v3-main-docs-dev.json \
  --output assistant/.cache/main-docs-new-new.json
```

MLflow runs: old/old 491666f45c7747ca8b4fb4f14e08317f; current/old
4f20daf31a154d738c9c4149cc932ad9; current/reviewed
6ed9499f026c42e49c3bf714bb2db81b. The reviewed fake-generation baseline is
committed; no real-model generation was run for this refresh.
