# Portable evidence presentation

On 2026-11-04, local machine metadata was removed from the **current presentation**
of recorded evidence: repository/home paths in logs and commands, and hostname
attributes in JUnit XML. Test names, results, counts, timings, commit identities,
financial values and existing financial-artifact digests are unchanged. The 26
existing JUnit documents were compared as parsed XML; only hostname metadata
changed. The existing four artifact checksum lists now identify the redacted XML
copies, rather than implying their bytes remain identical to the raw capture.

`$REPO_ROOT`, `$WORKSPACE_ROOT` and `$USER_HOME` are literal display placeholders in
historical evidence. They do not mean those environment variables were set during
the original run. The commands preserve the executed options and parameters; set
your own directory values before reusing them. `redacted-host` is a presentation
label, not the machine on which a historical test ran.

The unredacted original evidence remains in Git commit `9af388f` at the same paths.
For example, `git show 9af388f:evidence/REVIEW/2026-11-03/junit.xml` retrieves that
raw historical capture. This cleanup **does not remove local paths from Git
history** and does not claim the repository is ready to become public. The remote
remains private. Public distribution of the existing history would require a
separately reviewed history rewrite or a clean export; neither is performed here.

The 14 supplied Pro documents, their manifest and `follow-up-review.md` remain
byte-for-byte unchanged. Only the derived `docs/reference/review.md` presentation
and its provenance have portable links and local paths redacted. Its original
SHA-256 and Git locator are preserved in the provenance; `docs/reference/SHA256SUMS`
now describes that redacted review copy. The learning README now accepts an
explicit document interpreter; tutorial prose, renderer and PDF are unchanged.
These are presentation changes, not rewritten conclusions or new passing tests.

For future text evidence, copy raw output to a new destination using the standard
library tool. Keep raw material outside the published repository, review the copy,
and record that its display metadata was redacted:

```sh
python ops/evidence_redact.py /private/tmp/raw-pytest.xml /private/tmp/portable-pytest.xml \
  --prefix "$PWD" '$REPO_ROOT' --prefix "$HOME" '$USER_HOME' \
  --hostname "$(hostname)"
```

The output must not already exist. Input and output must differ. The tool validates
UTF-8 JSON/XML before publication, rejects symlinks and binary/oversized input, and
creates a new mode0600 copy. It handles literal paths, file-URI percent escaping,
JSON spelling and XML escaping. It does not edit the raw source or replace an
existing destination. All paths to be redacted must be explicitly supplied: review
the result for other host-specific directories or metadata before committing it.
The tool is outside the production wheel, but is included with operational source
and its unit checks.

This is not a credential scrubber. Do not apply it to immutable financial exports,
signed provider evidence, source manifests, database dumps or golden data; changing
those bytes invalidates their integrity contract. Secret scanning and publication
visibility are separate controls.
