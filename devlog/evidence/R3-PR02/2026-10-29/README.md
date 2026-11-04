# R3-PR02 verification

Base: `b839ce65e2e35ec9b1e412e7bb8375991f6a43e9`. Locate this delivery with
`git log -1 --format=%H -- evidence/R3-PR02/2026-10-29/verification.json`.

**900 tests passed, no failures/skips,90.56s** in the unchanged locked Python3.12.14
environment. Actual PostgreSQL18.6, psycopg3.3.6 and independent SQLite3.53.1 provider
were used. Ruff check/format pass for96files. Wheel/sdist build; actual rebuilt wheel
installed into a separate environment, with its import location and006SQL checked.
See [commands, versions and boundaries](verification.json).

The206added cases cover local admission/transport, independently constructed
verifier snapshots, parser/HTTP bounds, live PostgreSQL maintenance and actual
provider processes. The test matrix verifies literal80/62/18 balances, oneHELD/no
payoutjournal, retained202, genuine concurrentadmission and real wire-level unknown
COMMIT recovery. Worker calls use their dedicated login and transport observes an
IDLE connection after durableclaim. A canonicalbutwrong600 instruction cannot leave
the process when the captured business request/hold say62. Slowheader/body peers
cannot extend the bounded client attempt indefinitely; owned sockets/timers close.

Rebuild derives posted totals and activeholdtotals independently, preserveshold
history, and records all reservedbefore/after values withfold_version2. Oldv1–v4
exports andfold1 audits retain their previous rules. Genuine old005upgrade with
unattributed nonzeroreserved is rejected rather than erased. Existingcurrentformat
R2 expectations change wherev5 now treats unattributedreserved as repairable A drift;
oldformat fixtures remain unchanged.

Tests discovered and corrected one migration constraint-name mistake; payload
hashguards were explicitly tested afterward. The firstwhole diagnostic run had
896passes/3outdatedscopeexpectations, and overlapped finalbindinghardening. It is not
the finalproof; the frozen900case rerun passed. No oldtest was skipped. Historical
001–005, reference originals, frozenfixtures, lab and tutorial were not modified.

This slice does not apply externalterminal financialresults. The202 is admission,
not paymentconfirmation. Reportv5 explicitly excludes externalresultauthority; no
realbank, arbitraryfailureliveness, productioncapacity or personalmastery claim.
R3-PR03/04 and laterreleases remain active authorized work. Finaltestowned resources
were cleaned; the pre-existing learningcontainer was preserved.
