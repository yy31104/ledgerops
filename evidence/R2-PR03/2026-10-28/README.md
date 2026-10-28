# R2-PR03 verification — 2026-10-28

Final: **571 passed**, zero failures/errors/skips, JUnit elapsed **39.585s**.
272 unit, 41 reference, 258 real PostgreSQL integration cases. 123 new cases relative
to the separate resume correction (69 pure, 54 real-component). [Final JUnit](pytest.xml).

## Revision and scope

Base `3d4901f8629007e0ce0a57f15f6a0102f21a1b2c` contains only the separately requested
resume-policy correction, with its own 448-pass evidence. This commit adds PR03
under the accepted original-merchant/full-reversal decisions. No PR04 completion claim.

Find this implementation commit with
`git log -1 --format=%H -- evidence/R2-PR03/2026-10-28/verification.json` and inspect
`git diff 3d4901f..<that-commit>`. [Metadata](verification.json) binds tested Git trees
and package-tooling blobs; the final message reports the containing commit hash.
No new SHA source list. Existing data hashes and historical evidence are preserved.

The diff adds a typed command, finite shared purchase/reversal transaction/retry/
replay, reusable sorted balance locks and transfer SQL, migration005, exportformat4,
independent reversal verification and tests. Public purchase API and canonical bytes
are unchanged. Existing R1 financial tests, migrations001–004, reference model and
frozen command corpus match base. Legitimate existing test extensions: migration
count/fixture, future-schema label and format4 intent-scope/empty-reversal-table
expectations. New unit cases were appended to export/verifier tests.

## Observable checks

| Property | Evidence |
|---|---|
| Frozen business trace | Drive literal fixtures/golden/r2_commands.json through actual app credentials. Merchant140/reserved100 refuses80; fund40, old R-A replays422, new R-B succeeds, new R-C returns422 ALREADY_REVERSED. Check every step's statuses/replay/counts/reserved, final cumulative totals and success relation, plus byte-identical original purchase rows. |
| Authority and scope | Original customer/other merchant/unknown actor cannot submit/replay; unknown/OPENING/rejected/reversal originals cannot admit; cross-workspace, stored-owner bypass, changed canonical request and cross-kind collisions do not disclose or mutate. When the actor owns both relevant objects, conflicts retain named409 behavior. |
| Retained-state integrity | Original payload, response and exact entries are validated; corrupt originals abort. A forged ALREADY_REVERSED with no actual success relation can satisfy DB structure but is refused by service for same/new keys and flagged by B. No new request association survives. Historical insufficient-funds results are not re-decided. |
| Lock dependencies | Observe original FOR UPDATE wait after own claims; second different ID waits for original and returns saved ALREADY, same-key/business races replay. A NOWAIT probe establishes original-before-balances and UUID order rather than debit-role-first order. One success relation/journal remains. |
| Database final-state guard | Direct actual app SQL attempts incomplete accepted reversal, rejected financial effects or a different declared total. Statements execute, actual COMMIT raises23514 and all added rows roll back; original result remains unchanged. |
| Atomic writes | Eight owner-created fault stages exercise attempt, entry, projection, effect, unexpected23505, operation, request and deferred COMMIT. Exact SQLSTATE/diagnostics and IDLE rollback asserted, complete financial state equals before-state. Remove guard; same identity succeeds once. Unique failures are not converted into business replay. |
| Wire COMMIT ambiguity | Actual proxy prevents COMMIT forwarding, or observes DB commit but withholds acknowledgement. Reconnect with the same original command/IDs resolves both, preserving one effect; new-key replay adds only a request binding. |
| Bounded retry | Two40001 aborts recover, three exhaust three total attempts;23514 never retries. Backoff observes IDLE and unchanged aborted state. Existing actual deadlock, cancellation, lock-timeout and purchase recovery suite remains green. |
| Independent verification | Stdlib-only parser resolves original accepted purchase independently, checks inverse accounts/amount/body, request route bindings and all attempt/success relations. Wrong refund account or amount with matching projection passes A, fails B. Rejection history is not recomputed from today's balance. |
| Export/maintenance integration | Zero-reserve successful reversal history exports/validates; drift then freeze/preview/live repair/resume yields literal100/100/0/0, correct epoch, healthy saved report, same success relation and unchanged original. Nonzero-reserve golden trace passes structure/A/B while reservation support remains explicitly false. |
| Compatibility and permissions | Exact frozen v1/v2/v3 bytes verify with source and freshly installed CLI. Format4 includes attempts/effects; old versions cannot silently carry them. App history remains immutable; maintenance/reader cannot create reversal relations. |

The independent verifier imports only stdlib; it uses no production domain,
canonicalization, posting or SQL helper. DB constraints do not prove owners/request
intent/projection correspondence. The service validates original exact financial
instruction and B separately checks historical results. Privileged SQL is an explicit
trust boundary, not a property inferred from balanced entries.

## Commands, packaging and transparent failures

`UV_CACHE_DIR=.cache/uv uv run --locked --offline python -m pytest -q --junitxml=evidence/R2-PR03/2026-10-28/pytest.xml` produces the final total above.

The [first full run](pytest-first-run.xml) has 570 passes and one failure: the PR01
reader integration test asserted intent_scope only PURCHASE. Newformat4 declares
PURCHASE/REVERSAL capability even when a snapshot contains only purchases. The updated
assertion also requires format4 and empty attempt/effect tables for that sample;
the rerun passes all571. No financial expectation was relaxed. Old-format unit and
frozen-bundle capability tests remain unchanged.
The raw failed JUnit retains pytest's assertion-spacing line, including trailing
whitespace; source/docs diff checks exclude that unchanged diagnostic artifact.

During new fault-test setup, a PL/pgSQL combined AND referenced NEW fields absent on
other table types; changing to nested IF made each stage exercise its intended fault.
This was test instrumentation, not a production failure. Exact final diagnostics,
rollback and recovery assertions passed for every stage. No arbitrary timeout or
unexpected23505 is counted as a successful business result.

`UV_OFFLINE=1 make check` passes Ruff and format (58 files), force-installs current
modules including public ReversalCommand/submit_reversal and all five SQL resources.
`make build`, installed CLI help and offline checks of frozen formats1/2/3 pass.
[Actual output](checks.log), [package inspection](package-check.json),
[versions/cleanup](environment.json). Wheel production bytes match tested files;
sdist includes new tests and compatibility fixtures, excludes private references.
No post-test production/test edits; documentation/evidence changed afterward and
the final README was rebuilt/inspected.

## Boundary and next work

No HTTP/token adapter, persistent demo DB, external provider/refund, partial refund,
reservation lifecycle or R5 approval. The trusted caller authenticates Principal;
UUID possession is not authority. Corrected resume gates structure/A only while
retaining B/reservation issues; repair gates remain strict. Old schema/report
interpretations remain historical rather than acquiring new guarantees retroactively.
No additional filesystem hardening was added to PR01's sufficient local protocol.

R2-PR04 must still assess integrated purchase/reversal/maintenance schedules and the
whole R2 acceptance list. Finite forced schedules/fault locations do not prove every
possible failure. No push, tracker or Chronicle edit. Accepted choices do not establish
Yi's independent mastery; implementation and personal ownership remain separate.
