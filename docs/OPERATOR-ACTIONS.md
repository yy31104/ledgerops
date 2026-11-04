# Controlled reprocessing of terminal evidence

R5-PR03 exposes one action: `REPROCESS_TERMINAL_LOCAL_BLOCKER`. The provider already has an authenticated
SUCCEEDED, FAILED or CLOSED_ABSENT observation, but local financial processing stopped with LOCAL_BLOCKER. After an administrator
completes an independent repair and publishes fresh R4 reconciliation evidence, two actors review the action and retry local settlement using the original observation.
This entry point sends no provider request. UNKNOWN, contradictory evidence and EVIDENCE_LIMIT cannot be bypassed with this action.

## A complete example using 62

The customer starts with 100. A purchase of 80 leaves the merchant with 80. A payout of 60 plus a fee of 2 reserves 62, leaving 18 available.
The provider reports SUCCEEDED, but local settlement fails. The original observation, LOCAL_BLOCKER, hold and 202 receipt all remain.

1. Repair the local fault without changing the original observation or incident. Export again and publish a complete R4 version. The successful payment's source must appear in the actual
   provider manifest; an old empty manifest cannot authorize the action merely because its version is new.
2. The proposer submits the payout, original terminal observation, current run UUID and reason. The server reads the amount, payment key, complete incident set,
   target accounts, hold and observation versions, then creates an immutable proposal and SHA-256 digest. Repeating the same action ID preserves the original bytes and absolute expiry.
3. A different actor reads and approves the proposal. Changing tokens or clients does not let the proposer approve their own action. Approval itself transfers no money.
4. Execution rechecks expiry, restore generation, current maker/checker permission versions, the workspace's current evidence epoch,
   referenced run and all files, source HMAC, the full current target and related contradictions. Only then does it call the existing settle path.
5. SUCCEEDED consumes 62: the merchant has booked 18, reserved 0 and available 18, with the existing merchant-debit/clearing-credit entries.
   FAILED/CLOSED_ABSENT releases 62 without payment entries. Settlement, execution result, attempt and incident-resolution relations commit in the same transaction.

Original incidents are never deleted. `payout_incident_resolution` appends only the exact LOCAL_BLOCKER incidents handled by this action.
Workers and queries use `unresolved_payout_incident` to distinguish unresolved from resolved incidents; a recent 32-item summary cannot authorize clearing them.

## Service configuration and interfaces

An ordinary HTTP instance has no operator entry point by default. Configure all four settings only on a separate operator instance:

| Setting | Requirement |
|---|---|
| LEDGEROPS_OPERATOR_DSN | Separate login inheriting ledger_operator; superusers and members of migrator, auth_admin or ledger_app are rejected. |
| LEDGEROPS_OPERATOR_OBJECTS | Existing, non-symlink R4 object root; all referenced files must be retained. |
| LEDGEROPS_OPERATOR_NAMESPACE | Explicit provider namespace UUID. |
| LEDGEROPS_OPERATOR_KEYS_FILE | Owner-private key file using provider trust rules; a request body cannot supply trust roots. |

ledger_operator includes worker/reader/auth_reader privileges for trusted code and is not distributed to end users.
HTTP still derives workspace, actor, client and scopes from the Bearer credential's database mapping. A request cannot select the database login.
The ordinary service DSN still follows HTTP-SERVICE configuration. Public instances must not mount the operator DSN or keys.
The restore deployment flag blocks all operator POST requests; reading existing audits is unaffected by the write flag.
Place the operator instance behind a restricted entry point. Hiding controls in the frontend is not permission isolation.

| Interface | Permission and content |
|---|---|
| POST /v1/operator/actions | approval:propose; exact fields action_id/action_type/payout_id/observation_id/evidence_run_id/reason/ttl_seconds. |
| GET /v1/operator/actions/UUID | evidence:read; proposal, approval, execution and latest 20 attempts, with attempts_have_more. |
| POST /v1/operator/actions/UUID/approve | approval:approve; only proposal_sha256; actor must differ from the proposer. |
| POST /v1/operator/actions/UUID/execute | approval:execute; only proposal_sha256 and a stable execution_id. |

New proposals and approvals return 201; original-identity replays return 200. Successful execution returns 200.
409 means a version, evidence or another bound fact has changed; 422 means the business action cannot execute.
Definite 409/422 execution refusals are retained as stable failed attempts in a new controlled transaction after the financial transaction rolls back.
Unknown database or COMMIT errors propagate without fabricating an execution-failed record; query or retry using the same action/execution IDs.
Once an action has a successful execution, it returns only the retained result and cannot transfer money again.

The financial POST Idempotency-Key is not used here: action_id identifies the proposal and execution_id identifies the execution attempt.
Every retry retains the original IDs. Changed proposal content requires a new action ID; a different intent cannot reuse an old identity.

## Reviewing in the console

The investigation console enables controls using current `/v1/me` permissions. Users with operator scopes but without accounts:read can also connect.
It presents the sequence: propose → another actor reads the original text → execute. Amounts and epoch digests travel as strings.
`proposal_json` retains the exact canonical JSON string rather than being parsed as JavaScript numbers and serialized again. The browser calculates
SHA-256 over the original bytes and requires a match before approval. A proposal hash does not prove a person read it; the database still performs all current-state checks.

Credentials, the current proposal and execution ID remain only in page memory. Disconnecting or navigating away clears them, and old asynchronous responses cannot revive a session.
After switching to the checker, enter the retained action UUID again to read the original proposal. Do not share approval tokens in URLs or files.
Browser automation was restricted by the Work settings: actual HTTP flows and script syntax were checked; visual appearance and real-browser interaction have not been accepted.

## Locks, invalidation and resource limits

The cooperative code lock order is: shared workspace barrier → registry → shared auth → provider namespace → action → original operation/payout/
intent → hold → balances ordered by account UUID. Freeze takes the exclusive workspace barrier and waits for those transactions to finish.
Whether external payments have stopped must still be checked with the provider. Tests cover two executors, both orderings of callback/cross-workspace contradictions and freeze waiting.
They do not establish that arbitrary future SQL is deadlock-free. Only definitely aborted 40P01/40001 transactions are retried.

Proposals bind the workspace-wide evidence epoch, so a new publication for another date also invalidates them. Spending a target balance, adding an observation,
changing restore generation or maker/checker membership/client versions, or reaching the database expiry all require fresh review.
Rotating a token alone does not change the same actor's permission version; the executor's current credential must still be valid on every attempt.

Proposals are limited to 512KiB, and the complete LOCAL_BLOCKER set to 100 incidents; 101 incidents reject the whole action. Each action allows at most 100 execution attempts.
Expiry is 1–3600 seconds, with 15 minutes used by the page. Referenced objects are limited to 64MiB in total before review; oversized sets are rejected before reading.
Workspace exports retain their own separate capacity limit, with chunked export deferred to a later operational slice. There is no automatic garbage collection or silent history loss.

## Independent checks and limits

Financial export v9 adds approval, execution and resolution relations. Report v3 independently checks canonical SHA, distinct actors and exact
terminal/effect/incident binding. Old v1–v8 exports retain their original bytes and capabilities. Only a validated LOCAL_BLOCKER resolution stops blocking settlement.
Exports exclude credentials, auth tables and secret configuration in operator-attempt reasons. An original request in the current database still does not prove external original intent.

The offline verifier has neither permission history nor trust keys. It cannot establish that the maker/checker was authorized at the time or claim to have authenticated the HMAC.
Reports explicitly retain OPERATOR_HISTORICAL_AUTHORIZATION and PAYOUT_PROVIDER_AUTHENTICITY as unverified items.
Online execution uses actual current permissions and configured trust roots. Finite fault tests do not establish arbitrary failure schedules, bank finality or multi-region high availability.
