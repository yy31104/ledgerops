# Contribution rules

- Keep changes within the requested scope and preserve unrelated work.
- Start with README.md and the relevant service or operations documentation.
- Use Python with explicit SQL; keep financial transaction boundaries visible.
- Do not perform network I/O inside financial database transactions.
- Preserve journal history and stable business IDs and request keys.
- State which guarantees belong to Python, database constraints or independent verification.
- Add forward migrations; never edit an existing SQL migration.
- Keep migration credentials separate from runtime credentials.
- Preserve fixtures/golden/ and all existing SQL migrations.
- Keep personal notes, teaching tools, process records and generated documents outside Git.
- Keep credentials, local environments and caches outside Git.
- Run `make bootstrap` to install the locked development environment.
- Run `make check` for lint, formatting and installed-package checks.
- Run `make test` for the full suite; real PostgreSQL tests require Docker.
- Integration tests must fail clearly when Docker is unavailable.
- Keep expected financial results independent of production calculation helpers.
- Report commands, outcomes and remaining limits accurately.
- Do not push, publish or deploy without user authorization.
