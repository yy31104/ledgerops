# Proposal: readable names for internal codename files

Status: **proposal only, nothing renamed yet.** This page lists the target names,
every reference that would have to change, and what must stay as it is.

## Where the codenames come from

- **R2** was the second development milestone. Its commits are tagged `R2-PR01` to
  `R2-PR04`: consistent export and independent verification, projection maintenance,
  full purchase reversal, and integrated acceptance. The tag says when a file was
  written, not what it tests.
- **STREAM10** is "complete bounded financial stream, format 10". The number is a real
  format version, but the file name hides that it documents the export format.

Other tests in `tests/integration/` already describe the behaviour, for example
`test_payout_atomicity_mutations.py` and `test_purchase_concurrency.py`. The names
below follow that convention.

## Renames

| Current path | Proposed path | Why this name |
|---|---|---|
| `docs/STREAM10.md` | `docs/STREAM-EXPORT-FORMAT.md` | Says what it documents; matches `EXPORT-AND-VERIFY.md` and `STREAMING-EVIDENCE.md`. The title keeps "format 10" |
| `tests/integration/test_r2_export_interruption.py` | `tests/integration/test_export_killed_before_publication.py` | Its only test kills the exporter before publication and checks that old exports are unchanged |
| `tests/integration/test_r2_mixed.py` | `tests/integration/test_purchase_reversal_interleaving.py` | It runs observed interleavings of purchases, reversals, freeze and replay |
| `tests/integration/test_r2_mutations.py` | `tests/integration/test_reversal_mutations.py` | Mutation experiments on reversal safeguards; same pattern as `test_payout_*_mutations.py` |
| `tests/integration/test_r2_review_boundary.py` | `tests/integration/test_reversal_disabled_customer.py` | Its single test pins the disabled-customer eligibility boundary for reversals |
| `fixtures/golden/r2_commands.json` | `fixtures/golden/reversal_commands.json` | Hand-calculated command corpus for the "full reversal rejected, then funded" story |

## References to update in the same commit

These are all of them, from `git grep -n -E "STREAM10\.md|test_r2_|r2_commands"` on `main`:

| File | Line | Change |
|---|---:|---|
| `docs/EXPORT-AND-VERIFY.md` | 203 | link `STREAM10.md` → `STREAM-EXPORT-FORMAT.md` (link text can stay "STREAM10" or become "stream format 10") |
| `docs/RELEASE-READINESS.md` | 79 | link `STREAM10.md` → `STREAM-EXPORT-FORMAT.md` |
| `reference/README.md` | 4 | path `fixtures/golden/r2_commands.json` → `reversal_commands.json` |
| `tests/integration/test_reversal_admission.py` | 45 | path in `ROOT / "fixtures/golden/r2_commands.json"` |
| `tests/reference/test_command_model.py` | 13 | `CORPUS` path |

Nothing else refers to the test files by name. `Makefile`, the CI workflow and
`pyproject.toml` only use directories, and `fixtures/golden/SHA256SUMS` lists only
`purchase_80.json`.

## What must NOT be renamed

These look like the same codenames but are format or history identifiers. Changing
them would break stored data or checksums.

| Identifier | Where | Why it stays |
|---|---|---|
| `--format stream10`, `--source-format stream10` | `src/ledgerops/cli.py`, `recon_cli.py` | Public CLI values that scripts and docs already use |
| `FINANCIAL_STREAM10`, `FINANCIAL_STREAM10_COMPACT_V1` | manifests read and written by `exchange.py`, `stream_reconciliation.py`, `operator_evidence.py`, `reconciliation/publication.py` | Written into exported manifests; old exports must stay readable |
| `-- R2-PR01` / `-- R2-PR02` comments | `src/ledgerops/db/sql/002_*.sql`, `003_*.sql` | Migration bytes are checksummed (`schema_migration.sha256`, `make check`); never edit historical migrations |
| `"fixture_id": "R2-full-reversal-rejected-then-funded-v1"` | inside the fixture | Content identity of the corpus; renaming the file does not require changing its contents |
| `stream10_129_diagnostics_…` check name | `ops/linux/acceptance.py` | Label inside retained acceptance reports |

## How to execute later

1. Wait until no open branch touches these files. Another session is currently
   changing `tests/`.
2. In one commit, only `git mv` the six files. In a second commit, update the five
   references, so `git log --follow` and review both stay simple.
3. Run `make check` and the full `make test`. Then check that nothing still mentions
   the old names:

   ```bash
   git grep -n -E "STREAM10\.md|test_r2_|r2_commands"
   ```

   It should return nothing; the identifiers in the table above are matched only as
   `stream10`/`STREAM10` without `.md`.
4. Optional: put a one-line stub at `docs/STREAM10.md` pointing to the new page for one
   release, so external links keep working.

Risk: low. These are file paths only, with no runtime or format change. The main
failure mode is a missed path in a test, and the test suite catches it immediately
because both corpus loaders read the file at import or test time.
