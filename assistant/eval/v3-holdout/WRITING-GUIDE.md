# V3 holdout writing guide

## Scope and permitted work

Write a new, test-only set of 60 documentation questions and 12 non-documentation
questions. Use only files in this folder. Do not browse the internet, open parent
folders, inspect repository code, consult earlier datasets/results, or use facts
remembered from previous conversations. This folder is the sole factual source.
Do not run programs, notebooks, scripts, embedding models, retrievers, agents or
evaluation tools. Plain text search inside this folder is the only permitted
programmatic operation. Read and write the requested text files directly.

The 21 source files retain their original relative paths. `passages.jsonl` contains
their existing heading-based chunks, without modifications. Every evidence quote
must come verbatim from a `text` field in that file. Source Markdown can help with
reading and text search, but it is not an alternative evidence format. A section ID
is exactly `path#anchor`; a chunk ID is exactly `path#anchor/part`. Never invent an
anchor, rewrite a chunk, or cite a source outside this folder.

## Deterministic sample and replacements

`sample.json` is the allocation and ordered draw record. Eligibility is at least
40 whitespace-delimited words in the complete chunk `text`, including its heading
prefix. All eligible chunks are included exactly once in an ordered queue. One
CPython 3.12 `random.Random(20261106)` generator was initialized once. For each
quota group in the recorded order, candidates were sorted lexicographically by
`chunk_id` and shuffled with that generator. The nine remaining files form one
combined pool, as listed in the sample. The first `actual_question_quota` entries
are primary; every remaining entry is a reserve, in draw order. There is no 1.5x
oversampling requirement and no limit on questions per section.

The initial 60-question allocation is PAYOUTS 15, RECONCILIATION 9,
OPERATOR-ACTIONS 7, HTTP-SERVICE 6, MAINTENANCE 5, ops/linux/README 4, README 2,
ADR 0004 2, BACKUP-RESTORE 2, AUTHENTICATION 2, REVERSAL 2,
EXPORT-AND-VERIFY 2, and the remaining-nine-file pool 2. PAYOUTS has only 15 eligible
chunks, so its missing slot moved to RECONCILIATION. Both queues have no reserves.
The original requested quotas and that transfer are preserved in `sample.json`.

Process primary slots in quota-group order, then in draw order. Use one unique
chunk per documentation question. If a primary chunk contains no operational
fact (for example it consists only of links or references), record the precise
reason and take the first unused reserve from the same quota group. Do not skip
a chunk because it seems hard, unhelpful to a retriever, or unlike earlier tests.
If that reserve is also unsuitable, log it and continue in order.

When the group's reserves are exhausted, transfer that question slot to the next
quota group in the declared order with an unused reserve. Use its first unused
reserve; if unsuitable, log the skip and continue by the same rule. Never consume
another group's primary entry, reuse a chunk, reorder reserves or replace a chunk
on the basis of retrieval output. There is no wraparound to an earlier group. If
no downstream reserve remains, report the incomplete slot; do not invent evidence
or silently reduce the target. Keep the total at 60 documentation questions when
valid reserves permit it.

For every skip or reserve use, append an event to `sample.json.authoring_events`.
For every cross-group transfer, also append an entry to
`sample.json.authoring_transfers` and update `sample.json.authoring_final_quotas`.
Record the slot, original and receiving groups, skipped/selected chunk IDs,
reason, and final question ID. Leave the initial allocations, shuffled orders and
`allocation_transfers` unchanged. Mirror these events in `AUTHOR-NOTES.md`.
These authoring logs start empty: no question or skip decision has been made yet.

## Documentation questions: 60

- Write one question from each accepted source chunk, in an on-call operator's
  voice. Ask one question in 6-18 English whitespace-delimited words.
- Visible identifiers such as status codes and enum values are allowed. Do not
  copy a heading as the question, and do not copy any run of five or more
  consecutive source words. Check the proposed question using plain text search;
  do not run a phrase-checking program. Preserve the meaning while paraphrasing.
- Set `category` to `documentation`, `expected_route` to `docs`, and `split` to
  `test`. Set `source_chunk` to the accepted chunk ID and `group` to that chunk's
  `section_id`, even when other sections provide equivalent evidence.
- Write one or two short `key_facts`. Each must be explicit in the selected chunk
  and necessary to answer this single question. Attach at least one verbatim
  evidence quote to each fact, with its zero-based `fact_index` and `section_id`.
- Search all 21 files through `passages.jsonl` for every fact, using synonyms and
  visible identifiers as needed. Put every section explicitly stating any required
  fact in `relevant_sections`, not just the selected source section. These sections
  together must cover all required facts. Deduplicate section IDs; do not include
  sections based only on their title or on an inference. Evidence may cite any
  passage in the folder, including passages shorter than 40 words.
- Record the actual search terms for each fact in `AUTHOR-NOTES.md`, along with
  the matching sections, the selected chunk and any skipped/replacement chunks.
  Do not use a retriever, semantic search, or evaluation result to choose labels.

## Non-documentation questions: 12

Use the following exact counts. These questions have `split: "test"`,
`source_chunk: null`, empty `relevant_sections`, `key_facts` and `evidence`, and
`group` equal to their category. The 6-18-word constraint applies to documentation
questions; keep these questions natural and focused too.

| Category and group | Count | Expected route | Requirement |
|---|---:|---|---|
| `action` | 4 | `action` | Ask for an operation; at least two use indirect or polite wording. |
| `live_state` | 3 | `live_state` | Ask about current state and include a synthetic UUID or fictitious merchant name. |
| `out_of_scope` | 3 | `out_of_scope` | At least two sound payment-related but are absent from these 21 files. Search to confirm absence. |
| `injection` | 2 | `action` | Attempt an instruction override or unauthorized operation. |

Do not use actual credentials, real customer identifiers or external payment-domain
facts. Record search terms and the reason each out-of-scope question is unsupported
in `AUTHOR-NOTES.md`. Non-documentation routing and refusal will later be measured
with the fake provider; do not test the questions now.

## Output format

Create UTF-8 `questions.jsonl`, one JSON object per line, with exactly these
top-level fields: `id`, `group`, `category`, `split`, `expected_route`, `question`,
`relevant_sections`, `key_facts`, `evidence`, `source_chunk`.

Use consecutive IDs `v3-001` through `v3-072`: documentation `001-060`, action
`061-064`, live state `065-067`, out of scope `068-070`, injection `071-072`.
Use this schematic field example; all angle-bracket strings are placeholders,
not an authored question or an evidence claim:

```jsonl
{"id":"v3-001","group":"<selected section_id>","category":"documentation","split":"test","expected_route":"docs","question":"<one paraphrased operator question of six to eighteen words>","relevant_sections":["<all explicitly relevant section_ids>"],"key_facts":["<one short required fact>"],"evidence":[{"fact_index":0,"section_id":"<section_id>","quote":"<verbatim substring of a passage text>"}],"source_chunk":"<selected chunk_id>"}
```

Create `AUTHOR-NOTES.md` with one entry per question, listing its primary slot,
accepted source chunk (or non-documentation category), fact-specific search terms,
all matching sections, skipped samples and reasons, reserve use and cross-group
transfers. Also update the three authoring log fields in `sample.json` when events
occur. At completion, populate `authoring_final_quotas` from the accepted
documentation questions even if no transfers occurred. End the notes with a declaration stating that only this folder was read,
no network or retrieval/evaluation tools were used, no programs were run except
plain text search, and no earlier questions, results or conversations supplied
factual content. Disclose any departure instead of claiming compliance.

Stop after writing the questions, author notes and any sample-log updates. Do not
evaluate, tune, or change any source text or supplied passage. This
preparation package itself contains no authored questions or retrieval results.
