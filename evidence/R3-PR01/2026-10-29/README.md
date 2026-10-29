# R3-PR01 verification

Base: `eacbad9cc1e70c4a1754b6355a35eb5def64018a`. Find this delivery commit with
`git log -1 --format=%H -- evidence/R3-PR01/2026-10-29/verification.json`.
See [machine-readable record](verification.json) for actual versions and command.

**85 tests passed in9.16s**, with no failures/skips, in an environment recreated
strictly from existing `uv.lock`. New-source Ruff check and format check pass.
The final run supersedes preliminary cached-dependency runs whose transitive
versions did not all match the lockfile. No dependency version/lockfile changed.

48 store/protocol tests exercise literal financial fields, restart configuration,
strict JSON/HMAC, immutable terminal transitions, concurrent SQLite writers and
fixed-watermark history.14 pure HTTP tests cover input/control validation.
23 actual subprocess/HTTP tests exercise token separation, replay after restart,
bounded inputs, concurrent same-key submissions, both PUT/close race orders,
pending and failed outcomes, and external SIGKILL after observed pre/post-COMMIT
gates. Independent SQL reads check effect counts and60/2/62 values. Fixtures join
owned child processes and retain the SQLite file only for the intended restart.

SQLite3.53.1 actually reports DELETE, synchronous2(FULL), foreign_keys1,
busy_timeout2000ms and quick_check=ok. These are measured settings, not a claim
about every filesystem/power failure. Original ledger source, migrations, lab,
tutorial and reference originals are outside this slice.

## Complete regression and package validation

After restoring the tracked iCloud placeholders, the entire suite passed:
**694 passed, no failures/skips,167.77s**. Ruff check and format pass for all82
Python files. Wheel and sdist build; a separate clean environment installed the
wheel plus the existing locked psycopg3.3.6/typing-extensions4.16.0 versions and
verified imports and SQL001–005 resources. Wheel excludes provider_sim/lab/tests;
sdist includes provider_sim for its tests and excludes the learning-page files.

The initial full run had687passes/7failures: five corrupted/offline build-cache
failures and two Git-status timeouts while iCloud files were downloading. An
additional sandbox-only run lacked Docker/socket access, and an interrupted
continuation left one owned container that was subsequently removed. None of
these attempts is called a passing run. The final full run used the locked temp
environment with UV_NO_SYNC to avoid rebuilding from the evicted repo cache;
installation was separately verified from the actual wheel, outside the checkout.
Source/test assertions were not weakened. The pre-existing learning container
was preserved; the completed suite cleaned its own processes and containers.
No sync settings or unrelated processes were changed.

This verifies the independent provider boundary only. Ledger hold/admission,
dispatch deadline scheduling, authenticated result resolution, activity-hold
projection repair, R4–R6, production authentication and deployment remain pending.
Personal understanding is not assessed by these tests.
