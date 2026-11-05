# Retrieval evaluation

Can the assistant find the right part of the docs, and does it refuse what the docs
cannot answer? This page reports one run of
[`evaluation.py`](../src/ledgerops_assistant/evaluation.py) on the draft question set.
The full record, with every question's ranking and answer, is
[`eval/results/2026-11-05-draft-set.json`](../eval/results/2026-11-05-draft-set.json).

**Status: draft set.** The questions, labels and the routing rules were written by the
same author, so routing accuracy here is an upper bound. The set becomes v1 after a
review and after adding questions written by someone who has not seen the router.

## The question set

[`eval/questions.jsonl`](../eval/questions.jsonl) has 71 questions:

| Expected outcome | Count | Examples |
|---|---:|---|
| Answer from the docs | 57 | "Does a provider timeout release the reserved funds?" |
| Point to the live API instead | 3 | "Was payout 4000…0003 paid?" |
| Refuse to act | 3 | "Release the hold on payout 4000…0003." |
| Out of scope | 8 | "Which transaction isolation level does MySQL use by default?" |

Each answerable question lists its relevant sections as `path#anchor`, the same form a
citation takes. Labels name sections rather than chunks, so a new chunker does not
invalidate them. Paraphrases of one question share a `group`, and a group sits wholly
in `dev` or `test` (36 / 35 questions). The only thing tuned on `dev` is the scope-gate
threshold.

## Retrieval

Corpus: README, `docs/`, ADRs and the Linux operations guide at commit `b44e0da`;
175 chunks in 105 sections, median 148 words per chunk. Scores are over distinct
sections, best first.

| Method | Split | n | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@10 |
|---|---|---:|---:|---:|---:|---:|---:|
| BM25 | dev | 29 | 0.55 | 0.79 | 0.90 | 0.75 | 0.67 |
| BM25 | test | 28 | 0.64 | 0.82 | 0.89 | 0.84 | 0.74 |
| Vector (bge-small) | dev | 29 | 0.55 | 0.79 | 0.90 | 0.71 | 0.68 |
| Vector (bge-small) | test | 28 | 0.50 | 0.86 | 0.89 | 0.79 | 0.67 |
| Hybrid (RRF) | dev | 29 | 0.62 | 0.83 | 0.86 | 0.72 | 0.72 |
| Hybrid (RRF) | test | 28 | 0.68 | 0.82 | 0.89 | 0.83 | 0.77 |
| Vector, no query instruction | dev | 29 | 0.48 | 0.72 | 0.83 | 0.67 | 0.63 |
| Vector, no query instruction | test | 28 | 0.43 | 0.86 | 0.86 | 0.76 | 0.64 |

With 57 questions, one question moves Hit@1 by 0.018. Paired bootstrap over all 57
(2,000 resamples) shows which differences survive that noise:

| Change | Metric | Mean difference | 95% interval |
|---|---|---:|---|
| BM25 → hybrid | Hit@1 | +0.053 | [−0.070, +0.175] |
| BM25 → hybrid | MRR@10 | +0.041 | [−0.048, +0.127] |
| Vector → hybrid | Hit@1 | +0.123 | [+0.018, +0.228] |
| No instruction → instruction | Hit@1 | +0.070 | [+0.018, +0.140] |
| No instruction → instruction | MRR@10 | +0.038 | [+0.006, +0.075] |

What this run supports:

- **BM25 is a strong baseline on these docs.** They are full of exact identifiers such
  as `LIMIT_EXCEEDED`, `MAY_HAVE_SENT` and `/v1/metrics`, and operators ask with those
  identifiers. BM25 and vector search tie at Hit@5.
- **Hybrid improves on BM25 for 17 questions and is worse for 8, but the interval
  includes zero.** This set cannot show that hybrid beats BM25. It does show hybrid beats
  vector search alone at Hit@1.
- **Vector search earns its place on plain wording.** On the three questions that avoid
  the docs' vocabulary ("the bank says the money went out, but the system still shows it
  as blocked"), MRR@10 is 0.19 for BM25 and 0.56 for vectors. Three questions is an
  illustration, not a measurement.
- **The query instruction helps.** bge-small was trained with a prefix for
  question-to-passage search; leaving it off costs about 0.07 Hit@1.
- **Exact search is enough.** The index is 175 × 384 floats (269 KB). A query takes
  about 3 ms, almost all of it embedding the question; the matrix product is well under
  a millisecond. An approximate index such as HNSW would add recall loss for no gain at
  this size.

Misses at Hit@5, by method:

| Method | Questions with no relevant section in the top 5 |
|---|---|
| BM25 | q004, q020, q024, q035, q044, q055 |
| Vector | q015, q023, q024, q044, q049, q052 |
| Hybrid | q015, q018, q024, q030, q035, q044, q049 |

Two questions defeat every method:

- **q024**, "What makes an operator proposal invalid…": the docs say "require fresh
  review", not "invalid".
- **q044**, "Which endpoint shows the journal entries…": the answer is one row of the
  API table, inside a chunk that mostly covers other routes.

The second points at the chunker, which keeps a whole table in one chunk, rather than
at the label.

## Refusals

Routing rules run before retrieval ([`routing.py`](../src/ledgerops_assistant/routing.py)).
They sent all 3 action requests and all 3 live-state questions the right way. As noted
above, they were written alongside these questions.

Out-of-scope questions go through a **scope gate**: refuse when the best vector match
has cosine similarity below a threshold chosen on `dev`. The run chose 0.6098. It
refused 5 of 8 out-of-scope questions and wrongly refused no answerable question. It
also shows why a similarity threshold cannot do this job alone:

| Out-of-scope question | Best cosine |
|---|---:|
| Which transaction isolation level does MySQL use by default? | 0.703 |
| How do I configure PayPal payouts to a bank account? | 0.678 |
| How do I set up Stripe webhooks for refunds? | 0.616 |
| How do I reset my GitHub password? | 0.610 |
| How do I reduce Kafka consumer lag? | 0.608 |
| Lowest-scoring answerable question | 0.616 |

Similarity measures topic, not whether a passage contains the answer. A question about
payouts or isolation levels lands near the payout and transaction docs. The gate stays
as a cheap first filter for clearly unrelated questions. Judging answerability is the
model's job (rule 1 of the [prompt](../src/ledgerops_assistant/providers/base.py)), and
that needs a real provider to measure.

## What these numbers do not show

- **Answer quality.** The run used the `fake` provider, which copies the two sentences
  that overlap most with the question. Its citation hit rate (0.70) measures that
  heuristic, not an assistant. A real-model run is the next measurement.
- **Questions from real operators.** All questions are synthetic and in English;
  bge-small-en is an English model.
- **Stability over doc changes.** Labels are checked against the corpus on every run
  (an unknown section fails the run), but nothing yet re-runs the evaluation when docs
  change.

## Reproduce

```sh
cd assistant
uv sync --locked
uv run --locked python -m ledgerops_assistant.evaluation --output eval/results/run.json
```

The first run downloads bge-small (about 134 MB) at pinned revision
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` and caches chunk vectors under `.cache/`.
`--embedder hashing` runs the same pipeline offline with meaningless vectors; CI uses it
to test the plumbing, never to report quality.
