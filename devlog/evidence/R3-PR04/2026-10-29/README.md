# R3-PR04 verification

Base `ff9a70ea98afecbbcd736080ec224a1838770ee0`. Locate delivery using
`git log -1 --format=%H -- evidence/R3-PR04/2026-10-29/verification.json`.

**1114 passed,188.67s, no failures/skips.** This adds94 cases:62config/runtime unit
checks and32actual PostgreSQL/process/mutation cases. Ruff111files, wheel/sdist and
actual separate-environment installedworker checks pass. [Full evidence](verification.json).

The worker is a real process with its own bounded PG connection and external
restore/dispatch switches. Tests observe providerCOMMIT, SIGKILL the ledgerworker,
read independentSQLite effects and PG hold, restart, then check one effect/journal/
consumption and immutable202. Another test kills afterdurableclaim beforeprovider
receipt, obtains a tombstone afterdeadline and proves a delayedPUT cannotpay.

Actual waitgraphs cover hold/refund bothorders, settlement/purchase and freeze
waiting for claim/observation. Actual workerGUCs and identity are observed; restored
NORMAL alone cannot permit network. SIGTERM paths and testfixtures clean owned
connections/processes; pre-existing learningcontainer remains untouched.

Eight named defect classes are deliberately injected, with businessassertions
rejecting them. Two atomicity mutants are prevented by SQLdeferredguards; others
expose wrong funds/evidence directly or through independent folds. They are not
syntax-error or watchdog-timeout successes. First process-test watchdog was shorter
than the real30s schedule; tests now wait45s without shortening production delays.

R3 passes its finite synthetic/local contract. No production bank, arbitraryfailure
liveness, oldbackupmissinghistory or personalmastery claim. R4–R6/service/operations
remain authorized ongoing work. SQL001–007 and learning/reference artifacts unchanged.
