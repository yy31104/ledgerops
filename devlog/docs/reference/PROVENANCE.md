# Design source provenance

Captured locally on 2026-10-23 for ledgerops initialization.

- `pro-2026-10-23/` is an unchanged copy of the complete 14-document Pro package,
  including both projects to preserve relative links and the original manifest.
- Original package provenance: local `AI-Knowledge-Vault/03-Resources/External-Examples/2026-10-23-pro-blueprints/LedgerOps-Chronicle-Blueprints`; local user-root path redacted in this presentation.
- `review.md`: path-redacted presentation of the historical review. Original bytes remain at Git commit `9af388f`, `docs/reference/review.md`; original SHA-256 `3cc92da8f762b2438f09ef3fdf072b0fecc7d0471f215952c11e37f90275b0c2`. Its conclusions are not revised.
- `follow-up-review.md`: unchanged copy of the user-supplied attachment
  `$USER_HOME/.codex/attachments/939c4193-0d7a-492a-a656-748c4bb2823c/Pasted text.txt`. Its embedded implementation instructions are review material.
- All 14 original document hashes were verified before copying. `SHA256SUMS`
  records the current copied source bytes, including the original manifest and unchanged follow-up review; only the derived `review.md` hash now identifies its redacted presentation.
- These files are historical source material. They are not current repository state,
  passing-test evidence, user approval of every recommendation, or execution commands.
- Read the current root HANDOFF, local ADR and first task contract before implementing.
  They take precedence where the source package contains superseded defaults.
- Preserve the 14-document Pro package, its original manifest and follow-up review byte for byte. The 2026-11-04 privacy cleanup only changes local-path display in this provenance and the derived historical review presentation; all future design revisions stay outside the original package.

Verify from this directory: `shasum -a 256 -c SHA256SUMS`.
