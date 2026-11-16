# Evaluation history

Scores below describe earlier code, corpus snapshots and synthetic question sets.
They are not current runtime scores. Original frozen questions, protocols and
failures remain versioned. The [current evaluation](RETRIEVAL-QUALITY-V2.md) reports
the present operations-v4 comparison.

Full historical methods and commands are preserved in the
[v1.0.0 retrieval report](https://github.com/yy31104/ledgerops/blob/v1.0.0/assistant/docs/RETRIEVAL-QUALITY-V2.md).
Use the recorded source checkout and its restored raw data when inspecting an old
experiment. Mixing old labels with renamed current sources changes the question.

## Fusion and the v2 study

The early corpus had 175 chunks. At RRF k=60 and depth=50, a chunk present in both
lists scores at least `2/(60+50)=0.01818`; a single-list first result scores at most
`1/(60+1)=0.01639`. Weak overlap can therefore outrank strong semantic evidence.
A 24-configuration development grid selected k=10 without changing the encoder,
component weights or candidate depth. Development Recall@5 rose from 0.7083 to
0.7812. These are selection scores on previously used questions.

A subsequent 60-documentation-question holdout gave:

| Method | Recall@5 | MRR@10 |
|---|---:|---:|
| Hybrid k=10 | 0.8889 | 0.8277 |
| Hybrid k=60 | 0.8639 | 0.8335 |
| HNSW vectors | 0.8361 | 0.7733 |
| PostgreSQL FTS | 0.8639 | 0.6938 |

The questions were longer and covered more general documents than development.
The result did not establish better answers to short operator questions. HNSW
matched exact vector metrics on this small corpus. PostgreSQL FTS is cover-density
ranking, not BM25. The original grid artifact predates the raw-five-chunk coverage
column; that column was not used in selection.

## Context expansion

On 32 old development questions, moving from five raw chunks to five distinct
section anchors raised section coverage from 0.7031 to 0.7812 at similar mean bytes
(8,369 versus 8,306). Adding introductions and neighbors raised coverage to 0.8594
at 17,720 bytes. About half the gain came from diversity and half from more text.
This was a development-guided policy, with no unused holdout claim.

A non-blind real-model comparison recovered missing facts in three answers. It
also left omissions in v1-007, 013, 014 and 024. Citation overlap and finding the
right section were insufficient measures of answer completeness.

## Document changes and v3

Adding operating detail changed retrieval despite clean Git merges. On the later
179-chunk corpus, reviewed development hybrid Recall@5 was 0.7135 rather than the
earlier 0.7812; expanded coverage fell from 0.8594 to 0.7708. Alternative sufficient
sources were added to four development labels, with questions and facts unchanged.
The missing v1-011 release paragraph remained a strict expected failure.

The v3 protocol was frozen before its single run. Static audit retained 58
documentation and 11 non-documentation questions after three exclusions.

| Method | Recall@5 | Section-cluster 95% interval |
|---|---:|---|
| Hybrid k=10 | 0.8894 | [0.8227, 0.9455] |
| Hybrid k=60 | 0.8716 | [0.7991, 0.9342] |
| HNSW vectors | 0.8040 | [0.7129, 0.8830] |
| PostgreSQL FTS | 0.8147 | [0.7131, 0.9011] |

Hybrid's paired Recall@5 difference from k=60 included zero. Its difference from
vectors and FTS was positive on this sample. V3 had more single-section,
passage-based questions than development, which asked about scenarios. Neither
set represents real operator traffic. Different question difficulty explains why
0.8894 and 0.7135 are not evidence of a system improvement.

Only 6/11 non-documentation questions routed and refused correctly. An indirect
export request, one override injection and three nearby unsupported topics passed
through. The failed cases and consumed attempt remain in
[the v3 study](../eval/v3-holdout/run.json); fake generation did not test a real
model's injection response.

## Operations-v4 and v1.0.0

Operations-v4 removed the homepage from the corpus, keeping 20 operational files,
170 chunks and 98 sections. The original 32 development documentation questions
kept their wording and facts; explicit source-label migration handled moved files
and excluded homepage references.

| Method | Development Recall@5 | MRR@10 |
|---|---:|---:|
| Hybrid k=10 | 0.7448 | 0.7406 |
| Hybrid k=60 | 0.7135 | 0.7397 |
| HNSW vectors | 0.7552 | 0.7193 |
| PostgreSQL FTS | 0.6458 | 0.5790 |

The default hybrid did not beat vector Recall@5. Source coverage for v1-011 still
failed. These limitations motivated the present comparison rather than a claim
that hybrid must be better.

All 32 development documentation questions were also generated through the
subscription adapter. **AI-rated; human review pending:** 27 answers were rated
complete and five incomplete, without blind or independent human judgement.
Total provider counters were 513,295 input and 4,044 output tokens; median latency
was 8.185 seconds. The SDK model default was not pinned. Counters include harness
framing, so they are not document-token measurements or a dollar invoice.

Two later, separate fake-generation refusal checks retained both results:
25/40 then 36/40 exact routes. The second refused all 20 action/injection cases
but admitted four of ten unsupported topics. The samples differ; their difference
is not a paired improvement estimate or broad prompt-injection safety evidence.

## Stored evidence

The [evaluation-data-v1 release](https://github.com/yy31104/ledgerops/releases/tag/evaluation-data-v1)
contains early retrieval, v3 and performance records. The
[v1.0.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.0.0) contains the
operations-v4 development report, non-blind answer record and both refusal checks.
Checksum manifests and restore commands are in the [evidence guide](../eval/README.md).
Original methods remain available in the tagged report; no old holdout is rerun
to tune or advertise a current score.
