# Pending assistant stack review and corrections

Reviewed immutable head `964a64a1c455ac728d6ad30de53159f9775965d3` against
`2609ffc1c09c7802c4f6b2d96ae3678d55b48e3d` on 2026-11-05. PRs #9, #10, #11,
#12, #15 and #16 were open and unmerged. Review covered all 66 source/data items
in the security-diff inventory, related tests/docs/Docker inputs and supporting
core authorization/proposal controls. The core was supporting context, not a new
repository-wide financial-engine audit.

## Verdict by area

| Area | Result and supporting control |
|---|---|
| Caller authority and read tools | Fixed origin, GET-only tool map, UUID paths, no redirects/proxy environment; current identity/scope checked per read. Core independently enforces actor-owned payouts/accounts, client-owned receipts and workspace evidence permissions. |
| Model loop | Found a per-turn reauthorization gap: a planner could receive a previously authorized observation after its read scope was removed. Final output/new reads were blocked. Corrected before planning or cost reservation; deadline also checked before a subsequent read. |
| Durable review | Owner namespace includes workspace, actor and client. Run/draft/evidence hashes, unique owner/request identity, absolute expiry, session advisory locks and sync checkpoints preserve one draft. Expanded crash/concurrency tests confirm this behavior. |
| Draft eligibility and console parity | Found mismatched payout evidence and reason limits. Corrected below. These were unusable local handoffs; the core already rejected them, so they were not financial authorization bypasses. |
| HTTP/SSE and browser | Bearer authentication, bounded bodies/provider selection, checked text before SSE prose, owner-bound cancellation, escaped text rendering and in-memory credentials. Reason validation now matches the backend byte limit. |
| MCP | One stdio investigation tool, fixed assistant URL and fake generation; private regular credential file with owner/mode/size/no-follow checks, no token/URL/method tool argument. One adapter instance intentionally uses one configured operator identity. |
| Compose and image | Synthetic loopback demonstration; no PostgreSQL port publication, separate owner/runtime credentials, restricted runtime role, pinned images/actions/dependencies, non-root read-only application containers and explicit build exclusions. |
| Optional subscription adapter | Local opt-in only; HTTP/CI guards, API-key environment removal, ChatGPT account-type check, ephemeral/read-only/deny-all requests and process-group timeout. Mock contract tests cover flags and timeout cleanup; actual SDK/OS isolation and plan inference were not independently exercised. |
| Public repository hygiene | Tracked files and reachable history checked; no real credential or identifying host-local path found. Details below. |

The contextual security pass established no reportable financial read/write
bypass or new attacker-controlled model recipient. The planner's observed
revocation window is still corrected defensively. It processed data already
authorized for the same request; reproduction did not establish a new
lower-privilege read, cross-owner leak or financial mutation.

## Corrections

1. **Reauthorize before planning.** Reuse the current-owner/successful-observation
   scope guard before constructing a planner message or reserving inference cost.
   A recording-provider HTTP reproduction made two calls before the fix, with the
   protected marker in the second call after scope removal. After the fix it makes
   one call, with no post-removal disclosure and the same safe final refusal.
   Revocation, scope narrowing, actor/workspace/client changes and unavailable
   identity have regression tests. A late/cancelled planner cannot start another
   API read. Both planning and answer nodes recheck cancellation/deadline after
   the potentially blocking identity lookup, before reserving or generating.
2. **Match the console reason contract.** Require nonblank text, no NUL and at most
   256 raw UTF-8 bytes. Previously 512 stripped characters were allowed, so 257
   ASCII characters, 129 two-byte characters or a NUL could create a draft that the
   actual proposal API rejected. Preserve the original reviewed text instead of
   trimming it. The old implementation also retained the raw text; the correction
   applies to validation. Browser/backend share the byte/NUL rule and treat
   Unicode White_Space, C0 separators U+001C–001F and BOM as blank.
3. **Bind eligible evidence to the requested payout.** Require the selected
   investigation's object ID, payout operation ID and any supplied operation ID
   to match the normalized intent. Tool metadata now canonicalizes valid UUID
   spellings as well as the route. Reject refused investigations, malformed or
   incomplete projections, and incident lists containing incompatible blockers.
   These are advisory screening checks; the console remains authoritative for
   source authenticity, complete history and financial eligibility.

## Recovery and permission evidence

New PostgreSQL tests exercise a crash immediately after the draft insertion
commits but before the prepare checkpoint persists. Reconstructing the workflow,
retrying and resuming returns the original action ID, payload, digest and expiry,
with one row and no second generation. A concurrent full invocation receives the
running conflict; subsequent retry/resume preserves the same draft.

Tests additionally cover wrong review digests without consuming the interrupt,
workspace/client isolation, changed evidence/intent, revoked maker permission,
expiry, mismatched payout projections, failed/refused investigation results,
malformed completeness flags, extra incompatible incidents and raw/multibyte
reason boundaries. Completed replay/inspection returns historical local state;
it is not fresh financial authority. The existing console independently checks
current proposal, actor, expiry and evidence again.

The actual core-service acceptance pair still passes: caller-scoped timeout
investigation preserves the held amount, and the human test driver can submit a
valid handed-off proposal once and replay it. Neither the assistant nor the test
approves or executes the proposal.

## Public hygiene

The original reviewed head has 362 tracked files. Static text/path inspection and
a redacted Gitleaks scan of 57 reachable commits (about 8.53 MB) produced 11
generic-pattern matches. Each was triaged: one public file-content SHA-256,
ordinary documentation prose, and synthetic domain/idempotency request keys.
They are not deployed bearer/API credentials. No broad ignore rule was added.

Identifying directory-prefix matches occur only in deliberate redaction tests;
the email example uses the reserved `example.invalid` domain. Container paths,
generic setup placeholders and Docker-managed volume paths are distinct from a
developer's private host paths. Generated `assistant/.env`, local caches and
dependency trees are ignored/excluded and are not committed. Raw local scan
bundles, which include machine-specific metadata, remain outside the public repo.

## Verification

- Core `make check` and full `make test`: **1697 passed**.
- Assistant default suite: **109 passed**; PostgreSQL/real-encoder selection:
  **47 passed**.
- Baseline snapshots: **3 passed**; real authenticated API/handoff acceptance:
  **2 passed**.
- React type/production build and **3 frontend tests** passed.
- Browser: 129 two-byte characters disable preparation; 128 enable it. A valid
  synthetic draft reaches awaiting-review, can be dismissed, and disappears on
  disconnect. The temporary QA tab was closed; no financial action was submitted.
- Default Compose rebuild reached healthy. Development retrieval quality gate
  passed; retrieval code, labels and frozen holdout results were not changed.

CI repeats the automated suites on the fix PR. The review does not assert
production load tolerance, semantic answer entailment, live external-provider
retention behavior, universal secret absence or independently verified SDK sandbox
isolation. Runtime role grants, configured origins and sensitive checkpoint-volume
retention remain deployment responsibilities.
