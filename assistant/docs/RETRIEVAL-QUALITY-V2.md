# Current evaluation (v1.2)

The corpus is **operations-v4: 20 files, 170 chunks and 98 sections**, digest
`822d084db36d3721a49fa80ca052722a6b0a1bd4d2fc2c4c81eb78628876a229`.
The homepage, navigation and article are outside the explicit source list.
Earlier results, including the full v1.1 comparison, are in the
[evaluation history](HISTORY.md). All labels are synthetic; none are human-labelled.

## Scope gate

Before v1.2 the gate had two checks: a short list of named external capabilities
and the cosine similarity of the best passage (threshold 0.5512). Similarity
compares the whole question with a passage, so a question about payout conversion
rates, blockchain confirmations or SMS notifications looks like a payout question.
Three of ten new unsupported topics passed in the v1.1 holdout.

v1.2 adds a coverage check that looks at the question's own terms against the
indexed corpus ([`coverage.py`](../src/ledgerops_assistant/coverage.py)):

- a code or acronym the corpus never uses (EUR, SMS, PSD2) refuses the question;
- otherwise terms are Porter-stemmed and weighted by passage IDF, with absent terms
  at the maximum weight. When absent terms carry at least **0.597** of the weight,
  the question is refused.

The vocabulary comes only from the corpus. Stop words are Snowball's English list
plus modal and request words from the routing grammar ("could", "explain"). The
check runs only on documentation-routed questions, before retrieval. The
named-capability list and the 0.5512 similarity threshold are unchanged.

The 0.597 threshold is the midpoint between the largest share among development
documentation questions (0.573) and the next out-of-scope share (0.621). Every
earlier question set counts as development data now, including the consumed v1.1
holdout. These numbers are therefore tuned on, not blind
([summary](../eval/v12-dev/scope-summary.json)):

| Development data | Gate | Out-of-scope passed | Documentation refused |
|---|---|---:|---:|
| Current development set | v1.1 | 4/12 | 0/110 |
| Current development set | v1.2 | 0/12 | 0/110 |
| All earlier sets, deduplicated | v1.1 | 9/37 | 0/193 |
| All earlier sets, deduplicated | v1.2 | 2/37 | 0/193 |

The two remaining passes (v1-053, a bank settlement cutoff in Warsaw; v3-068,
installment repayment) use only words the corpus also uses. Words with other
meanings in the corpus, such as "foreign" and "exchange", are a known weakness.

### Frozen check

Two separate source-only authors wrote new questions without the implementation
or any results; a separate static audit removed 31 near-duplicates of earlier
questions and one row with an incompletely labelled fact. The remaining 54
documentation and 34 out-of-scope questions (12 payment-industry topics, 14 product
features LedgerOps does not document, 8 general) were frozen in commit
[`8c1912a`](https://github.com/yy31104/ledgerops/commit/8c1912a37295e9d720771d90bd9da3c7fd1ec47c)
and run once ([protocol](../eval/v12-holdout/protocol.json),
[manifest](../eval/v12-holdout/manifest.json), [summary](../eval/v12-holdout/summary.json)).

| Gate | Out-of-scope passed | 95% Wilson interval | Documentation refused |
|---|---:|---|---:|
| v1.1 | 19/34 (55.9%) | [39.5%, 71.1%] | 0/54 |
| v1.2 | 10/34 (29.4%) | [16.8%, 46.2%] | 0/54 (upper bound 6.6%) |

Every question v1.2 lets through, v1.1 also let through; v1.2 refuses nine more.
By kind, passes fell from 5 to 1 of 12 industry questions and from 13 to 8 of 14
product questions, and stayed at 1 of 8 general questions. The largest uncovered
share among the new documentation questions was 0.483, below the 0.597 threshold.

**The gate still passes 10 of 34 unsupported questions.** Eight ask about product
features in LedgerOps' own words: a recurring weekly payout, an API rate limit,
notes on payouts, merging accounts, fee invoices, tips, gzip responses and a
license price. Their words all occur in the docs, so a vocabulary check cannot
see that the feature is missing. One asks about payment facilitators; a
cover-letter question passes on both checks. No rule changed after the run; these
questions are now development data.


## Retrieval and context

The retrieval default is still hybrid k=10, chosen by the preregistered v1.1
comparison (hybrid minus vector Recall@5 +0.0421, section-cluster CI [0.0089,
0.0898]; see [history](HISTORY.md#operations-v4-comparison-and-v110)). Nothing in
retrieval, fusion or context selection changed in v1.2.

On the 110 development documentation questions (the v1.1 development set plus the
consumed holdout; [baseline](../eval/baselines/v1.2-dev.json)):

| Method | Recall@5 | MRR@10 | nDCG@5 | Hit@1 | Hit@5 |
|---|---:|---:|---:|---:|---:|
| Hybrid k=10 | 0.8495 | 0.7878 | 0.7531 | 0.6636 | 0.9545 |
| Legacy hybrid k=60 | 0.8538 | 0.7882 | 0.7542 | 0.6636 | 0.9455 |
| pgvector HNSW | 0.8039 | 0.7738 | 0.7153 | 0.6545 | 0.9273 |
| PostgreSQL FTS | 0.7439 | 0.6459 | 0.6131 | 0.4818 | 0.8727 |

On the 54 new documentation questions of the frozen check, hybrid Recall@5 was
**0.9167**, section-cluster 95% CI [0.8426, 0.9737]; vector 0.8735, legacy hybrid
0.8889 and FTS 0.7685. The hybrid-minus-vector difference, +0.0432 with CI
[-0.0096, 0.1000], is not detectable on this sample. Hybrid exact quote coverage
was 0.8580 against 0.8765 for vector.

The `section-completion-v2` pack stays bounded at 18,000 bytes; its mean section
coverage is 0.8862 on development and 0.9259 on the frozen check. CI fails if development Recall@5, MRR,
nDCG, context coverage or documentation refusals get worse.

v1-007 has a new development label version: the sources state that replay returns
the retained refusal and that new operations check funds, but no indexed passage
says the new attempt also needs a new Idempotency-Key
([label changes](../eval/v12-dev/LABEL-CHANGES.md)). Earlier label files are unchanged.

## Token use

v1.1 recorded 32 subscription answers with a mean of 16,590 input tokens, while an
empty-evidence call used 17,149. v1.2 measures the parts directly
([script](../scripts/token_attribution.py), [summary](../eval/v12-token-attribution.json)):

- **Local counts.** The o200k_base tokenizer counts our instructions, schema,
  passages and question. `codex debug prompt-input`, run locally from the bundled
  CLI, renders the input that Codex adds; only section names and counts are kept.
- **Controlled calls.** 24 calls through the subscription adapter with
  `gpt-6.1-sol`: the same question (v1-001) with 0, 3, 6 or all 11 passages, doubled
  instructions, and empty evidence, in the v1.1 adapter configuration and with
  Codex extras turned off.

The local counts match the model's counters exactly: input tokens rise by 1.000
per locally counted passage token (no residual), and doubling the instructions
adds 333 tokens, the local count. Every call was a single model request.

Split of the 32 v1.1 answers (mean tokens per answer):

| Part | Tokens | Share |
|---|---:|---:|
| Instructions, developer note and answer schema | 450 | 2.7% |
| Question | 13 | 0.1% |
| Retrieved passages and framing | 3,047 | 18.4% |
| Input added by Codex (skill list, multi-agent role, plugin list, notes) | 2,765 | 16.7% |
| Tool definitions and other request framing (remainder) | 10,316 | 62.2% |
| Reported input | 16,590 | |

The remainder was 6,217–6,225 tokens in 15 answers and 13,924–13,930 in 17. So the
SDK, not retrieval, is most of the input, and about half of the calls carried
roughly 7,700 extra tokens that are not in the rendered input. The controlled calls
reproduced this: in the v1.1 configuration, 3 of 4 empty calls used 9,440–9,442
tokens and one used 17,239; 3 of 4 full calls used 12,510–12,512 and one 20,305.
The v1.1 empty-evidence control landed in the larger mode while 15 of 32 answers
landed in the smaller one, which is why it exceeded the mean. Which component adds
the extra block was not isolated.

The adapter now turns off Codex skills, plugins, apps, multi-agent and media
tools, and the collaboration, permission and environment notes. With that, all
four empty calls used 4,731 tokens and all four full calls 7,801: 3,068 passage
tokens (39%), 464 of our own, 629 still added by Codex and 3,646 of tool
definitions and framing. Answers in both configurations were well-formed, cited
two or three supplied passages and opened with the same rule; no answer-quality
score is claimed for either. Retrieval is not the largest part, so the context pack was
not compressed.

The 32 v1.1 answers still have no human score. Their worksheet is a
[v1.1.0 asset](https://github.com/yy31104/ledgerops/releases/download/v1.1.0/human-review.csv);
judge v1-007 against label version 2. Subscription counters measure plan quota,
not a dollar invoice; no paid API was used.

## Reproduce

```sh
cd assistant
uv run --locked python scripts/evaluate_local.py --split dev \
  --baseline eval/baselines/v1.2-dev.json --output .cache/current-dev.json
uv run --locked python -m scripts.scope_dev --output .cache/scope-dev.json
```

The token script needs `uv sync --locked --extra subscription --extra measurement`,
`LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1`, a ChatGPT sign-in and the restored v1.1
answer record. CI gates only development metrics and static freeze integrity;
frozen holdouts never run in CI. Raw records are release attachments; see the
[evidence guide](../eval/README.md).
