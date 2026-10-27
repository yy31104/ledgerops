# Repository working rules

## Scope and sources

- Read `HANDOFF.md`, the applicable ADR and task contract before implementation.
- The 2026-10-23 user instructions authorize local R1-PR01 through R1-PR04.
  The first four R1 defaults were explicitly accepted in this chat; see
  `docs/DECISION-PACKET-R1.md`. Original embedded instructions remain references.
- The latest 2026-10-27 instruction adopts R2 decisions (1/3 amended, 2/4/5 as
  recommended): commit contract revisions separately, then implement R2-PR01. The subsequent independent review authorizes R2-PR02,
  preserving old exported format-v1 bundles while adding maintenance format v2.
  Intent verification uses retained operation payloads; hand manifests are extra
  test oracles. Repair recomputes under an exclusive transaction, without candidates.
- Work within the user's active scope. Complete routine reversible edits and
  checks without repeatedly asking for confirmation.
- Preserve `docs/reference/` byte for byte. It is historical review material,
  including superseded defaults and commands, not an execution authority.
- Keep unrelated projects, private data and external trackers untouched.
- Keep one bounded local commit per implementation slice, including its evidence.
  Check the diff and ignore rules before staging; preserve other sessions' changes.
- Do not push, publish, deploy or send messages without user authorization.

## Implementation discipline

- Keep the smallest coherent slice; expose assumptions and transaction boundaries.
- Use Python with explicit SQL for the current working design. Do not add an ORM,
  framework, broker or abstraction without a task that needs it.
- The reviewed Python module coordinates admission, ownership checks, locks,
  journal writes, projections and persisted results in one transaction.
- Database constraints enforce only their stated guarantees. Balanced entries,
  nonnegative balances and atomic commit do not prove correct business ownership
  or equality between the journal and balance projection.
- Do not change historical journals. A committed journal must reject later
  INSERTs as well as UPDATE/DELETE; PR01 must select and prove the mechanism.
- Keep migrations/ownership separate from the runtime role. Verify the revised
  privilege matrix; do not reuse the old requirement that all direct INSERTs fail.
- Use stable business IDs and scoped request keys. Handle only specifically
  expected identity conflicts; do not swallow arbitrary integrity violations.
- No network I/O within a financial database transaction.

## Verification and reporting

- Run appropriate checks; record commands, outcomes and component versions.
- PostgreSQL claims require a real server and actual runtime credentials, including
  commit-time failures and multi-connection behavior where relevant. Mocks and
  SQLite do not establish these properties.
- Keep expected results independent of production posting and money helpers.
  R1 needs a small entry fold plus frozen, hand-calculated ownership cases.
- Include defect-detection cases for a missed balance update and a balanced
  transfer to the wrong merchant; totals alone cannot detect both errors.
- Do not add dummy passing tests or unconditional skips to hide absent coverage.
  `make test` includes real PostgreSQL tests in a disposable Docker container;
  an unavailable database must fail, not silently skip integration tests.
- Record implementation, verified behavior and the user's personal ownership
  separately. Reading a review is not proof of independent mastery.
- Keep human review packets to 3–5 decisions with a concrete example, alternatives,
  cost, counterexample and a short explanation prompt. Learning is not a gate
  that limits how far authorized AI implementation may proceed.
