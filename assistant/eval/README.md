# Evaluation evidence

Current development labels: `v12-dev/questions.jsonl` (148 rows: the v1.1
development set plus the consumed v1.1 holdout, 110 documentation questions);
CI baseline: `baselines/v1.2-dev.json`. Only development metrics gate CI, including
documentation refusals by the scope gate. `v12-dev/LABEL-CHANGES.md` records the
one label change (v1-007, version 2). New homepage edits do not alter the explicit
operational corpus.

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

## Earlier release sample

The [v1.0.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.0.0)
contains `ledgerops-evidence-v1.0.0.tar.gz`: current development evaluation, the
32-answer subscription record and both once-run refusal samples, including the
failed first check. `release-evidence-manifest.json` pins its archive and four raw
files; `release-summary.json` keeps summaries. Use the same restore command with
that manifest after publication. `answer-review.json` contains per-question AI
judgements; the refusal study folders retain frozen protocols, data and
run controls. No claim of independent human labels is made.

## v1.1 comparison and answer worksheet

`v11-holdout/` contains frozen questions, protocol, manifest, the one-run record and
an aggregate summary. The 85-question study ran once after its freeze was pushed;
no held-out retrieval or generation runs in CI. `v11-dev/` extends development
without changing earlier frozen sets. CI binds its source and scope threshold.

The [v1.1.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.1.0)
contains `ledgerops-evidence-v1.1.0.tar.gz`, pinned by
`v11-evidence-manifest.json`: raw rankings, development reports, context ablation,
and the once-generated 32-answer subscription record. The separate
`human-review.csv` and `human-review.md` assets include source passages and blank
judgement columns. `mutation-evidence-v1.1.0.tar.gz`
contains pristine controls, exact patches, failed test IDs and checksum evidence.
All raw records stay in release assets; tracked summaries do not replace them.

## v1.2 scope study and token measurement

`v12-dev/scope-summary.json` compares the v1.1 and v1.2 scope gates on every
earlier question set, now development data. `v12-holdout/` holds the new frozen
questions, protocol, manifest (with the static audit and every removal), the
one-run record and an aggregate summary. `v12-token-attribution.json` summarizes
the controlled subscription calls and the per-answer split of the v1.1 answers.

The [v1.2.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.2.0)
contains `ledgerops-evidence-v1.2.0.tar.gz`, pinned by `v12-evidence-manifest.json`:
the development report, per-question scope decisions, the holdout record and the
raw token calls. Restore it with the command above and that manifest.

## v1.3 passage support

[`v13-scope/`](v13-scope/) contains the targets, frozen cohorts, source audits,
one-run receipts and aggregate results. [The evaluation report](../docs/RETRIEVAL-QUALITY-V2.md)
shows each round, including misses. CI verifies frozen inputs and receipts.

The [v1.3.0 release](https://github.com/yy31104/ledgerops/releases/tag/v1.3.0)
contains `ledgerops-evidence-v1.3.0.tar.gz`, pinned by `v13-evidence-manifest.json`.
Restore it to inspect individual decisions and development calibration.
