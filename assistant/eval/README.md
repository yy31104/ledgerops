# Evaluation evidence

Current development labels: `v4-dev/questions.jsonl`; CI baseline:
`baselines/operations-v4.json`. Only development retrieval metrics gate CI. New
homepage edits do not alter the explicit operational corpus.

Historical v1/v2/v3 question sets, manifests, protocols and one-run controls remain
versioned. Their source paths and code hashes identify historical snapshots, not
current runtime files. Do not rerun old holdouts to tune current behavior.

Raw historical reports and performance samples are release attachments on
[evaluation-data-v1](https://github.com/yy31104/ledgerops/releases/tag/evaluation-data-v1).
`summary.json` retains summaries; `evidence-manifest.json` pins archive and individual
file SHA-256 values. Restore outside the tracked source tree, from repository root:

```sh
python3 assistant/scripts/evidence_archive.py restore \
  --manifest assistant/eval/evidence-manifest.json --cache assistant/.cache/evidence
cd assistant
uv run --locked python -m pytest -m evidence
```

Restoration verifies archive size/hash, embedded manifest, member names and every
file hash before writing. It rejects links, path traversal and changed existing
files. Historical reproduction commands in retrieval/performance documentation
must use their original recorded code/corpus checkout and restored files at the
original relative paths. Do not combine old labels with renamed current sources.
The default unit suite does not download evidence; CI runs the explicit archive
verification separately. No v3 retrieval or generation runs in CI.

## Current release sample

The [v1.0.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.0.0)
contains `ledgerops-evidence-v1.0.0.tar.gz`: current development evaluation, the
32-answer subscription record and both once-run refusal samples, including the
failed first check. `release-evidence-manifest.json` pins its archive and four raw
files; `release-summary.json` keeps summaries. Use the same restore command with
that manifest after publication. `answer-review.json` contains per-question manual
assistant judgements; the refusal study folders retain frozen protocols, data and
run controls. No claim of independent human labels is made.
