# Current retrieval and answer evaluation

The current corpus is **operations-v4: 20 files, 170 chunks and 98 sections**.
Its digest is
`822d084db36d3721a49fa80ca052722a6b0a1bd4d2fc2c4c81eb78628876a229`.
The homepage, navigation and article are outside the explicit source list.
[Earlier evaluations](HISTORY.md) use their recorded snapshots, not this release.

## The comparison

Development contains the original 32 documentation questions plus 23 new,
source-audited documentation questions, with eight routing controls. The original
question wording and facts are unchanged. A separate source-only author produced
the new questions without retrieval results or implementation access. Static audit
removed flawed or near-duplicate items without rewriting their questions or facts.
These are synthetic labels, not independent human annotations.

The fresh holdout retains **55 documentation questions in 41 source-section groups
and 30 non-documentation questions**. Its [protocol](../eval/v11-holdout/protocol.json)
and [manifest](../eval/v11-holdout/manifest.json) bind the source, implementation,
questions, exclusions and pre-run development baseline.

The primary metric is mean section Recall@5. A section-cluster bootstrap resamples
primary source sections 10,000 times with seed 20261116 and retains question
weights. Paired question-bootstrap intervals are also reported. An interval that
includes zero means **no detectable difference**, not equivalence.

The default rule is fixed: select hybrid only when its mean Recall@5 difference
from vector is positive and the paired section-cluster 95% interval is entirely
above zero. Otherwise select vector. This prespecified selection is not an
independent validation of the winning method. No fusion, threshold, label or
context parameter changes after the held-out run.

| Method | Recall@5 | Section-cluster 95% CI | MRR@10 | nDCG@5 | Hit@1 | Hit@5 |
|---|---:|---|---:|---:|---:|---:|
| pgvector HNSW | 0.8218 | [0.7403, 0.8932] | 0.8127 | 0.7420 | 0.7091 | 0.9636 |
| Hybrid k=10 | 0.8639 | [0.7972, 0.9213] | 0.8114 | 0.7725 | 0.6727 | 0.9818 |
| Legacy hybrid k=60 | 0.8906 | [0.8351, 0.9393] | 0.8127 | 0.7817 | 0.6727 | 1.0000 |
| PostgreSQL FTS | 0.7685 | [0.6753, 0.8492] | 0.6696 | 0.6283 | 0.5091 | 0.9091 |

Hybrid remains the default under the fixed rule: its paired Recall@5 difference
from vector is **+0.0421**, section-cluster 95% CI **[0.0089, 0.0898]**. MRR and
Hit@1 differences from vector are not detectable. Legacy k=60 has a higher Recall
point estimate, but its paired difference from k=10 includes zero; this result
does not establish that k=10 is better. No settings changed after the run.

| Hybrid minus comparator | Metric | Mean difference | Paired question 95% CI | Paired section-cluster 95% CI |
|---|---|---:|---|---|
| Vector | Recall@5 | +0.0421 | [0.0082, 0.0885] | [0.0089, 0.0898] |
| Vector | MRR@10 | -0.0014 | [-0.0612, 0.0579] | [-0.0608, 0.0539] |
| Vector | Hit@1 | -0.0364 | [-0.1455, 0.0727] | [-0.1429, 0.0588] |
| Legacy k=60 | Recall@5 | -0.0267 | [-0.0752, 0.0073] | [-0.0760, 0.0070] |
| Legacy k=60 | MRR@10 | -0.0014 | [-0.0105, 0.0077] | [-0.0107, 0.0075] |
| Legacy k=60 | Hit@1 | +0.0000 | [0.0000, 0.0000] | [0.0000, 0.0000] |
| FTS | Recall@5 | +0.0955 | [0.0348, 0.1667] | [0.0370, 0.1683] |
| FTS | MRR@10 | +0.1417 | [0.0609, 0.2228] | [0.0659, 0.2250] |
| FTS | Hit@1 | +0.1636 | [0.0364, 0.2909] | [0.0484, 0.2857] |

The run completed once after freeze commit
[`9c7519f`](https://github.com/yy31104/ledgerops/commit/9c7519fbf8cbbb0c79c1140423b4e743a9af0c17).
Its 85 questions and recorded failures remain unchanged. All 55 documentation
questions reached fake generation, with no false refusal.

| Method | Raw-five coverage | Expanded-pack coverage | Exact quote coverage |
|---|---:|---:|---:|
| Vector | 0.8121 | 0.8542 | 0.8939 |
| Hybrid k=10 | 0.8579 | 0.8676 | 0.8455 |
| Legacy hybrid k=60 | 0.8845 | 0.8979 | 0.8636 |
| FTS | 0.7133 | 0.7812 | 0.7636 |

Quote coverage averages each question's covered fraction of its declared source
quotes: 55 questions, 79 quotes. Vector supplies more exact quoted fragments than
hybrid despite lower section Recall. Hybrid's delivered section coverage equals
its candidate-pack coverage, 0.8676, because all 55 calls reached generation.
This tradeoff prevents treating the selected retrieval metric as answer accuracy.

Section Recall@5 deduplicates the ranked source sections. Raw-five coverage counts
the first five chunks. Expanded-pack coverage counts the selected candidate pack;
delivered coverage counts only passages sent to generation and assigns zero to
pre-generation refusals. Exact quote coverage asks whether declared evidence
fragments are present in their matching section; it does not grade semantic facts.

## Routing and refusal

The cosine threshold comes only from development. Action requests are refused;
live-state requests are directed to the read API; unsupported topics should be
refused before generation. The live agent authenticates before classification and
checks the scope gate on documentation requests. Read tools remain fixed GET
routes under the caller's current token.

| Fresh holdout category | Cases | Correct route | Correct refusal |
|---|---:|---:|---:|
| Action, including injection | 18 | 18/18 | 18/18 |
| Out of scope | 10 | 7/10 | 7/10 |
| Live state | 2 | 2/2 | 2/2 |

Three unsupported topics still pass the scope boundary. TODO: name the three
recorded failures from the saved result. There are no false documentation refusals.
Fake generation measures these routing and refusal paths. It does not establish
real-model injection resistance, and a refusal miss grants no write capability.

## Context completeness

Finding a section does not mean finding the paragraph with its rule. The current
`section-completion-v2` policy completes selected sections before adding unrelated
neighbors. Hybrid also retains leading vector-only candidates when consensus
crowds them out. A generic selector favors conjunctions of visible state codes.
Chunks keep their original text, IDs and document boundaries; ranking is unchanged.
The serialized document pack remains bounded to **18,000 bytes**.

On the same 55 development questions, a before/after context ablation gives:

| Retrieval | Context policy | Mean section coverage | Mean JSON bytes | Mean rendered document bytes |
|---|---|---:|---:|---:|
| Vector | Previous | 0.8315 | 17,657 | 14,743 |
| Vector | Current | 0.8224 | 17,690 | 14,764 |
| Hybrid | Previous | 0.8715 | 17,667 | 14,844 |
| Hybrid | Current | 0.9048 | 17,677 | 14,822 |

This exposes a tradeoff: hybrid source coverage rises while vector coverage falls
slightly. All per-question retrieval rankings are identical before and after.
The raw ablation record has SHA-256
`c6e02811adf965a0ebd5eee588bf6c1b1635971b28b23c497c551f1644befe1f`.
It is development evidence, not a blind estimate or an answer-quality score.

Regression tests check that the missing conditions behind v1-007, 011, 013, 014
and 024 reach generation as original source passages, including the no-journal
release rule, bounded quarantine, proposal order and DST-day caveat. A source can
still fail to state every labelled fact explicitly. Passing these tests does not
prove the generated answer is complete; the old v1-011 expected failure is removed.

## Real answers and token use

The original 32 development questions are generated once through the official
Codex SDK subscription adapter with a pinned model. The exported judgement table
contains each question, required facts, actual source passages and answer; its
human judgement column stays blank. **AI-rated; human review pending** describes
the earlier non-blind 27/32 completeness rating. No new human score is available.

TODO: State the new attempt status, model, recorded failures and the blank-table
release asset. Add provider-reported input/output/cached tokens and latency.

A separate empty-evidence call uses the same model, instructions and schema. Its
input count provides a measured framing baseline; the difference from full calls
also contains question and formatting effects. It is not an exact tokenizer-level
allocation between SDK overhead and document tokens. Report rendered bytes
separately rather than treating bytes as exact model tokens. Subscription quota
usage is recorded per call; it supplies no per-request dollar invoice.

## Reproduce development, inspect the holdout

```sh
cd assistant
uv run --locked python scripts/evaluate_local.py --split dev \
  --baseline eval/baselines/v1.1-dev.json --output .cache/current-dev.json
```

CI gates only development metrics and static freeze integrity. The holdout runner
claims `run.json` exclusively before model/database setup; a later failure consumes
the attempt and preserves partial output. The consumed holdout is not rerun for
a better score. Use its release record to inspect ranks, failures and intervals.
Raw records are release attachments; Git keeps summaries and frozen inputs.
See [evidence storage](../eval/README.md) and [historical methods](HISTORY.md).
