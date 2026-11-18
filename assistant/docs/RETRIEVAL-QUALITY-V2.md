# Scope and retrieval evaluation

The local CLI's subscription verifier admitted **1/108 (0.9%)** unsupported
questions and refused **4/103 (3.9%)** documentation questions in round 7.
Hybrid Recall@5 was **0.9110** for both gates. That cohort met all three
[registered targets](../eval/v13-scope/protocol.json).

## Scope decisions

Each round uses its own frozen questions and compares the candidate with v1.2
on the same corpus. The rates below include 95% Wilson intervals. The targets
apply to the observed rates, with limits of 10% for unsupported admissions and
5% for documentation refusals. The round 7 refusal interval extends to 9.6%.

| Round | v1.2 unsupported admitted | Candidate unsupported admitted | v1.2 documentation refused | Candidate documentation refused | Targets |
|---|---:|---:|---:|---:|---|
| [1](../eval/v13-scope/round-1/summary.json) | 46/75 (61.3%) [50.0%, 71.5%] | 30/75 (40.0%) [29.7%, 51.3%] | 1/66 (1.5%) [0.3%, 8.1%] | 4/66 (6.1%) [2.4%, 14.6%] | Miss |
| [2](../eval/v13-scope/round-2/summary.json) | 63/95 (66.3%) [56.3%, 75.0%] | 16/95 (16.8%) [10.6%, 25.6%] | 2/96 (2.1%) [0.6%, 7.3%] | 7/96 (7.3%) [3.6%, 14.3%] | Miss |
| [3](../eval/v13-scope/round-3/summary.json) | 59/86 (68.6%) [58.2%, 77.4%] | 16/86 (18.6%) [11.8%, 28.1%] | 2/72 (2.8%) [0.8%, 9.6%] | 3/72 (4.2%) [1.4%, 11.6%] | Miss |
| [4](../eval/v13-scope/round-4/summary.json) | 31/74 (41.9%) [31.3%, 53.3%] | 9/74 (12.2%) [6.5%, 21.5%] | 4/75 (5.3%) [2.1%, 12.9%] | 10/75 (13.3%) [7.4%, 22.8%] | Miss |
| [5](../eval/v13-scope/round-5/summary.json) | 64/101 (63.4%) [53.6%, 72.1%] | 22/101 (21.8%) [14.8%, 30.8%] | 2/81 (2.5%) [0.7%, 8.6%] | 5/81 (6.2%) [2.7%, 13.7%] | Miss |
| [6](../eval/v13-scope/round-6/summary.json) | 75/109 (68.8%) [59.6%, 76.7%] | 2/109 (1.8%) [0.5%, 6.4%] | 2/144 (1.4%) [0.4%, 4.9%] | 11/144 (7.6%) [4.3%, 13.2%] | Miss |
| [7](../eval/v13-scope/round-7/summary.json) | 77/108 (71.3%) [62.2%, 79.0%] | 1/108 (0.9%) [0.2%, 5.1%] | 2/103 (1.9%) [0.5%, 6.8%] | 4/103 (3.9%) [1.5%, 9.6%] | Pass |

Rounds 1–5 used local passage scores. Round 5 measured standalone admission
with the scoring rule used by the HTTP service. A
[grammar correction](../eval/v13-scope/QUESTION-ROUTING.md) recognizes informational
question openings. Before passage support in round 7, that route admitted
77/108 (71.3%) unsupported questions and refused 2/103 (1.9%)
documentation questions.

The CLI option asks `gpt-6.1-sol` whether the supplied passages support the
question. For a positive decision, the application checks the passage ID and
the model-selected excerpt, ignoring whitespace differences. The
verifier and answer share the same evidence pack. The 56,000-byte pack included
a labelled excerpt for each required fact on 658 of 666 development questions
with quotes, up from 636 at 36,000 bytes. See the
[pack comparison](../eval/v13-scope/subscription-expansion-development.json) and
[verification method](../eval/v13-scope/SUBSCRIPTION-EVIDENCE.md).

The 178 verification requests in round 7 had a median latency of 5.97 seconds.
They used 2,529,198 input and 8,929 output tokens from the existing ChatGPT plan.
All verdicts passed the application's response checks.

The earlier checks refused two questions about FIFOs. The verifier refused two
more documentation questions whose labelled excerpts were missing from the pack.
The one unsupported admission cited the repair audit's contents in response to
a question about comparing it with a change ticket.

## Hybrid retrieval

Recall counts labelled sections in the first five distinct raw results on every
documentation question. Both gates use the same ranking. Paired intervals use
10,000 bootstrap draws grouped by primary source section.

| Round | Questions | Sections | v1.2 Recall@5 | Candidate Recall@5 | Paired change and 95% CI |
|---|---:|---:|---:|---:|---:|
| 1 | 66 | 41 | 0.9003 | 0.9003 | +0.0000 [+0.0000, +0.0000] |
| 2 | 96 | 56 | 0.9184 | 0.9184 | +0.0000 [+0.0000, +0.0000] |
| 3 | 72 | 35 | 0.9444 | 0.9444 | +0.0000 [+0.0000, +0.0000] |
| 4 | 75 | 42 | 0.9733 | 0.9733 | +0.0000 [+0.0000, +0.0000] |
| 5 | 81 | 45 | 0.9280 | 0.9280 | +0.0000 [+0.0000, +0.0000] |
| 6 | 144 | 57 | 0.9201 | 0.9201 | +0.0000 [+0.0000, +0.0000] |
| 7 | 103 | 50 | 0.9110 | 0.9110 | +0.0000 [+0.0000, +0.0000] |

## Answer review

Two separate GPT-6.2 Sol sessions rated the saved v1.1 answers and six scope
labels, and a third resolved the disputed scope label. The local answer review
found **30/32 complete, 2 incomplete, 0 incorrect**. Both sessions agreed on all
32 answer grades (Cohen's κ 1.00) and 5/6 scope labels (κ 0.60). See the
[review summary](../eval/v13-scope/answer-review-summary.json) and
[v1-007 label correction](../eval/v12-dev/LABEL-CHANGES.md).

The scope trials use synthetic questions. Rounds 6 and 7 use a real subscription
model to verify passages, followed by the fake answer provider.

## Token use

The 32 v1.1 answers averaged 16,590 input tokens. Codex framing and tools accounted
for 79%, and the passages accounted for 18%. In v1.2, controlled full-evidence
calls used 7,801 input tokens with the isolated adapter. The
[v1.2 report](https://github.com/yy31104/ledgerops/blob/v1.2.0/assistant/docs/RETRIEVAL-QUALITY-V2.md#token-use)
has the full measurements.

## Evidence

The [evaluation guide](../eval/README.md) links to the raw release archive and
explains hash verification. See [evaluation history](HISTORY.md) for past corpus
versions and retrieval comparisons.
