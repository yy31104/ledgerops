# R3-PR03 verification

Base `ca68c496d0d6029cda517139307cef5ec95b4692`. Locate delivery with
`git log -1 --format=%H -- evidence/R3-PR03/2026-10-29/verification.json`.

**1020 tests passed, zero failures/skips,107.09s.** Actual PostgreSQL18.6 and
independent SQLite3.53.1 processes; fixed Python3.12.14/psycopg3.3.6 environment.
Ruff105-file check/format, wheel/sdist and actual separately installedwheel checks
passed. [Exact commands, prior failures and boundaries](verification.json).

120 new cases cover52 independent verification cases and68 real-PG resolution/status
cases. Terminal evidence, financial effects and hold lifecycle commit together;
SQL/COMMIT fault injection and actual lost COMMIT responses prove the recorded
outcomes. Signed collisions retain both sides; unauthenticated garbage cannot stop
valid work. Peer workspace freeze is respected without reverse financial locks.

The independent verifier does not import the production payout decision engine.
It checks retained request intent, raw evidence bindings and local final relations;
it explicitly cannot prove HMAC authenticity or independent provider execution from
an unsigned export alone. Historical formats and repair records keep their semantics.

Two whole-suite diagnostic runs failed only the CLI report expectation (first old
v5 capability, then incorrectly ordered new labels); final exact v6 assertion and
frozen1020case run pass. A local-blocker exception-name error was found in focused
tests and fixed before full regression. Protected historical SQL001–006, reference,
fixtures, lab and tutorial unchanged; no pushed commit. Own resources cleaned,
pre-existing learning container preserved. PR04 process recovery and later releases
remain ongoing; this evidence is not a production activation or mastery claim.
