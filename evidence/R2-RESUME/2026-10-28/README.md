# Resume-policy correction — 2026-10-28

Base `d825d41196cdc169ef262eeaa17f9fa47fc68ebe`. User explicitly requested a separate
correction before PR03: resume gates only structure/A; B/reserve findings are saved,
not blocking. Application of projection repair retains the original stricter gate.

Result: **448 passed**, zero failures/errors/skips. 203 unit, 41 reference,
204 real PostgreSQL integration; [actual JUnit](pytest.xml) records elapsed time.
16 new tests: two real-policy cases and fourteen format/report/compatibility cases.
`make check` and `make build` pass; [actual log](checks.log). Wheel Python/SQL bytes
match source. Source revision identity uses Git, with tested objects in
[metadata](verification.json); no new SHA source inventory.

Actual maintenance login tests show reserved10/booked100 and consistent wrong-M2
postings can resume with full persisted warning reports. Before resume, repair is
still refused with RESERVED_UNSUPPORTED / INVALID_SOURCE and complete state unchanged.
Resume changes no financial rows; closed report edits fail; later reserve release
does not rewrite the historical report returned by same-ID replay. Existing
structure/projection, drain, atomicity and wire-COMMIT recovery tests remain green.

Migration004 appends resume_report and replaces the one-time resume guard, preserving
003 bytes and pre-existing legacy closed records. Format3 exports the report; frozen
format1/2 files remain accepted under their old schema contracts. Legacy null reports
are disclosed as absence, not fabricated history. Saved historical issues do not
become fresh verifier findings or suppress current B/reserve warnings.

Locate this containing correction commit with
`git log -1 --format=%H -- evidence/R2-RESUME/2026-10-28/verification.json`.
The committed record binds source/tests/fixture Git objects; the final delivered
hash is reported to the user. No PR03 code is part of this correction commit.
PR02's earlier evidence remains historical and unchanged. No push/tracker update.
