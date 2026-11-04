# LedgerOps — proposed normative contracts

**Status:** design only, checked on 2026-10-23. `MUST` and `MUST NOT` specify the recommended implementation, not behavior already implemented. User decisions remain open in [IMPLEMENTATION-AND-DECISIONS](IMPLEMENTATION-AND-DECISIONS.md). Requirement IDs below map to [VERIFICATION](VERIFICATION.md). SQL is a design fragment; transaction algorithms are precise pseudocode, not a tested migration.

## 1. Money and chart of accounts

### L-AC1 — monetary domain

One currency, `PLN`, exponent 2, explicitly **synthetic**. An operation amount is an integer in `[1, 9_000_000_000_000_000]` minor units. Fees are nonnegative integers in the same domain; `amount + fee` must fit the operation limit. API amounts are canonical decimal strings matching `0|[1-9][0-9]*` with field-specific positivity. Reject floats, exponent notation, signs, leading zeros, Boolean values, unknown fields and unsupported currencies. No foreign exchange or rounding conversion occurs in the posting core.

Per-entry amounts use `bigint`; accumulated debit, credit and hold totals use `numeric(38,0)`, with explicit bounds checks before writes. Operations cannot silently overflow at either layer. Every worked numeric example in this document uses **minor units**: 100 means PLN 1.00, 80 means PLN 0.80.

| Account class | Normal side | Booked balance | Allowed owner | Opening example |
|---|---|---|---|---|
| `SYNTHETIC_CLEARING` asset | Debit | debits − credits | Workspace/system | Debit 100 |
| `CUSTOMER_LIABILITY` | Credit | credits − debits | Customer UUID | Credit 100 |
| `MERCHANT_LIABILITY` | Credit | credits − debits | Merchant UUID | Starts at zero |

All these balances are nonnegative. Account identity, currency, class, normal side and owner are immutable. The first purchase API rejects identical source and destination account IDs; the unique-account-per-journal constraint agrees with that rule. A disabled account permits evidence reads but no new transfer/reservation. Settlement of an already executed payout must still be recorded after disablement; disabling cannot erase a liability or external fact. A future suspense/loss account needs a separately reviewed contract; it is not an escape hatch for failed tests.

### L-AC2 — postings, holds and conservation

| Event | Debit entry | Credit entry | Reservation effect |
|---|---|---|---|
| Seed customer with 100 | Clearing 100 | Customer 100 | None |
| Customer buys 80 from merchant M | Customer 80 | M 80 | None |
| M requests payout principal 60, fixed quoted fee 2 | **No posted journal** | **No posted journal** | Hold 62 on M |
| Provider authoritatively succeeds | M 62 | Clearing 62 | Consume the same hold 62 |
| Provider authoritatively rejects or seals non-execution | **No posted journal** | **No posted journal** | Release the same hold 62 |
| Full internal reversal of purchase 80 | M 80 | Customer 80 | None; M must have 80 available |

The proposed provider fee is charged to the merchant, included in the gross outflow, and retained in the business contract as `principal=60, fee=2, gross=62`. No fee-income claim is made for the platform. If the product later absorbs fees, introduce an expense posting and a different reserve contract rather than editing this interpretation.

For every journal: sum of debit amounts equals sum of credit amounts, within one workspace/currency. Globally, debit-normal signed balances plus credit-normal signed balances sum to zero. Customer balances alone do not sum to zero because the clearing contra-account matters. Available liability balance equals `booked − active_reserved`. A hold is not a posted transfer: its lifecycle is independently immutable, with a derived active total, and it must never be counted twice in conservation.

R1 reversal scope is one full reversal of a successful **internal purchase**, with a new operation ID referencing the original. The original journal is untouched. Reject an already reversed original or insufficient current merchant availability. Partial refunds, spent-money recovery, payout reversal and chargebacks are not covered. A business correction for the wrong owner is an explicitly reviewed new operation, never an `UPDATE` of old entries.

## 2. Schema and invariant enforcement

### L-DB1 — primary relations

All tables carry `workspace_id uuid`; composite foreign keys include it. UUID uniqueness by itself is not a tenant-authorization check. Timestamps are `timestamptz` in UTC for instants. Financial selection uses immutable IDs and transaction snapshots, not wall-clock ordering alone.

| Relation | Critical fields and keys | Constraints and indexes |
|---|---|---|
| `workspace` | `id`, `currency`, `schema_epoch`, `mode` (`NORMAL`, `FROZEN`, `RESTORE`) | Currency fixed PLN; schema epoch changes only by migration |
| `account` | `(workspace_id,id)` PK; `owner_id`, `class`, `normal_side`, `currency`, `enabled` | Unique class+owner where applicable; immutable ownership; class/normal-side CHECK |
| `account_balance` | `(workspace_id,account_id)` PK/FK; `debits`, `credits`, `booked`, `reserved`, `version bigint` | Totals nonnegative; `booked >= reserved >= 0`; `booked` updated using immutable normal side; only private entry points write |
| `operation` | `(workspace_id,operation_id)` PK; `kind`; canonical payload bytes and SHA-256; `admission` (`IN_PROGRESS`,`ACCEPTED`,`REJECTED`); `http_status`, `response_body jsonb`; `created_at`; `original_operation_id?` | Deferred check forbids committed IN_PROGRESS; rejection result persisted; original reference unique for full reversal; financial lifecycle separate |
| `request_result` | PK `(workspace_id,client_id,route_family,key)`; nonnull `operation_id`; canonical payload bytes/hash; persisted HTTP status/body | UTF-8 key 1–128 bytes; immutable terminal record; operation FK DEFERRABLE INITIALLY DEFERRED; deferred trigger requires complete status/body at commit; same-key insert serializes conflicts |
| `journal` | `(workspace_id,journal_id)` PK; `operation_id` UNIQUE; `kind`, `recorded_at` | One posted financial effect per admitted operation in this scope; append only |
| `journal_entry` | `(workspace_id,journal_id,line_no)` PK; account FK; `side`, `amount_minor bigint` | Positive amount, side D/C; unique account within journal; append only; index `(workspace_id,account_id,journal_id)` |
| `reservation` | `(workspace_id,payout_id)` PK/FK; `account_id`, `gross_minor`; current `state`; `version` | State HELD/CONSUMED/RELEASED; unique active business payout; no timer-based release |
| `reservation_event` | `(workspace_id,payout_id,event_no)` PK; transition, evidence ID, timestamp | Append only; legal transition enforced under payout/account locks |
| `payout` | operation FK/PK; `provider_namespace`, `provider_key` UNIQUE; principal/fee/gross; merchant; `knowledge`, `version` | Payload and key immutable; provider namespace/version pinned; contradiction flag is sticky until reviewed |
| `dispatch_intent` | payout FK UNIQUE; `state`, `next_due_at`, attempts, `last_attempt_id` | READY/IN_FLIGHT/OBSERVING/DONE/CANCELLED; indexed due work; created atomically with hold |
| `transport_attempt` | UUID PK, payout FK, started/finished, `MAY_HAVE_SENT` marker, response category | Append start then append/update bounded transport metadata; never directly determines financial success |
| `provider_observation` | `(namespace,event_id)` PK; payout key; sequence; signed payload bytes/hash; received time; source (`callback`,`lookup`,`close`) | Same event ID with different hash quarantined; retains contradictory evidence; index by payout+sequence |
| `recon_run` | UUID PK; immutable input manifest hash, rule/code versions, business cutoff; STAGING/COMPLETE/FAILED/SUPERSEDED; generation | One immutable result per run; publication CAS; previous versions retained |
| `object_registry_guard` | `workspace_id` PK/FK, created at workspace setup | Dedicated row lock serializes object promotion, registration, pin creation and GC; never a financial account lock |
| `evidence_object` | `(workspace_id,sha256)` PK; byte length, relative content path, schema/media type, created time, GC mark | Immutable bytes; validate canonical path and hash/length before registration; file alone is not a registered object |
| `evidence_pin` | UUID PK; workspace/object hash FK; owner type/ID; run/partition/generation if applicable; created time | Durable publisher, run, backup or reader reference; first version has no automatic expiry; abandoned pins require reviewed/quiesced cleanup |
| `recon_partition` | `(run_id,partition_id)` PK; expected group range/hash; generation, lease owner/until; state; result digest | Accepted partition result unique; generation checked at authoritative sink |
| `accepted_partition_result` | `(run_id,partition_id)` PK/FK; generation, owner, input manifest hash, result object hash FK | Immutable acceptance; exact duplicate returns prior acceptance, conflicting digest rejected |
| `recon_difference` | `(run_id,difference_id)` PK; category; stable business refs; evidence refs; expected/observed values | No automatic money correction; every result references fixed input version |
| `operator_action` | action UUID; immutable proposal; maker, checker; case/run/evidence versions; expected payout/account versions; state | Maker != checker; approved payload digest immutable; one execution effect keyed by action ID |
| `audit_event` | monotonic local ID/UUID, actor, action, reason, evidence refs, before/after version | Append only to runtime roles; do not claim tamper-proof against DB administrators |

Schema fragments expose the critical mechanism:

```sql
CREATE TABLE ledger_private.journal_entry (
  workspace_id uuid NOT NULL,
  journal_id uuid NOT NULL,
  line_no smallint NOT NULL CHECK (line_no BETWEEN 1 AND 16),
  account_id uuid NOT NULL,
  side text NOT NULL CHECK (side IN ('D','C')),
  amount_minor bigint NOT NULL
    CHECK (amount_minor BETWEEN 1 AND 9000000000000000),
  PRIMARY KEY (workspace_id, journal_id, line_no),
  UNIQUE (workspace_id, journal_id, account_id),
  FOREIGN KEY (workspace_id,journal_id)
    REFERENCES ledger_private.journal(workspace_id,journal_id),
  FOREIGN KEY (workspace_id,account_id)
    REFERENCES ledger_private.account(workspace_id,id)
);

-- Deferred row triggers schedule a GROUP check at COMMIT. The check is not a
-- row CHECK: it reads every entry in the affected immutable journal.
CREATE FUNCTION ledger_private.assert_complete_journal()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE n integer; net numeric;
BEGIN
  SELECT count(*), coalesce(sum(CASE side WHEN 'D' THEN amount_minor::numeric
                                      ELSE -amount_minor::numeric END),0)
    INTO n, net
    FROM ledger_private.journal_entry
   WHERE workspace_id = NEW.workspace_id AND journal_id = NEW.journal_id;
  IF n < 2 OR net <> 0 THEN
    RAISE EXCEPTION 'incomplete or unbalanced journal'
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER journal_complete_on_header
AFTER INSERT ON ledger_private.journal
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION ledger_private.assert_complete_journal();
CREATE CONSTRAINT TRIGGER journal_complete_on_line
AFTER INSERT ON ledger_private.journal_entry
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION ledger_private.assert_complete_journal();
```

The header trigger catches a journal with zero entries; the line trigger catches later incomplete lines. The trigger function is owned by the no-login function owner, with PUBLIC execution revoked and qualified objects, so deferred execution at commit can read private tables without granting the API role direct SELECT. Repeated checks within one commit are acceptable at the initial maximum of 16 lines. A BEFORE UPDATE/DELETE trigger rejects journal/header and entry mutation; `TRUNCATE`, table ownership and migration privileges are unavailable to runtime roles. Do not add a `draft` flag that lets incomplete posted journals remain committed.

The private posting function validates business-specific account classes/owners, computes signed account deltas, locks balance rows, checks resulting booked/available balances and overflow, inserts the complete journal, then applies all deltas. For each account, debit/credit totals, booked amount, reserved amount and version change in one UPDATE, so an immediate `booked >= reserved` CHECK never observes a transient half-update. For example, payout consumption updates merchant `debits += gross`, `booked -= gross`, `reserved -= gross` together. Deferred checks do **not** independently prove the balance projection correct; only permitted function writes, per-row bounds and independent drift/rebuild tests support that claim. Corrupting the projection through privileged test injection must be detected. A production migration owner can defeat these restrictions and remains a declared trust boundary.

### L-DB2 — privileges

`migration_owner` manages schemas; `ledger_function_owner` is NOLOGIN and owns narrowly scoped `SECURITY DEFINER` entry points. Runtime API/worker roles cannot assume either role. The schema boundary is explicit:

- `ledger_private` contains authoritative tables, constraint-trigger functions and private implementation functions. Runtime roles receive **no schema USAGE, direct table privileges or function EXECUTE** there. Revoke existing PUBLIC grants as well as direct grants; do not rely only on the defaults of newly created objects.
- `ledger_api` contains the exposed typed entry points and explicitly reviewed read views. Grant runtime roles USAGE on this schema, EXECUTE on their permitted function signatures, and SELECT only on the views appropriate to their role. Grant no CREATE and no blanket present/future function execution. Scope-sensitive reads use an authorized entry point unless a view's isolation policy has been separately verified.
- The API role may execute `ledger_api.submit_purchase`, `ledger_api.request_payout` and `ledger_api.reverse_purchase`. Each function owns **the entire admission algorithm**: request claim, operation lookup/admission, authorization recheck, posting or reservation, required intent, and persisted response. It invokes the private posting primitive internally. Worker claims, provider-observation transitions and later result publication likewise use narrowly granted `ledger_api` functions; Python workers do not gain private DML to implement those algorithms.

Entry points are explicitly `LANGUAGE plpgsql VOLATILE SECURITY DEFINER`, use schema-qualified objects, `SET search_path = pg_catalog, pg_temp`, typed arguments and no caller-controlled dynamic SQL. Revoke PUBLIC function execution, including owner default privileges for future functions, and revoke CREATE on shared schemas. Perform creation and privilege changes in the same migration transaction, so an unprotected function is not exposed between commits. The owner has the private access required by its functions; callers do not inherit direct private access.

The API server authenticates users and authorizes actor/workspace/operation access; its DB credentials are a trusted service boundary. SQL entry points validate scope and stored memberships, but do not magically authenticate an arbitrary actor argument from a compromised service credential. A freely settable session variable is not authentication. A new client replaying an existing operation must still be authorized to read that operation; knowing its UUID is insufficient.

### L-DB3 — projection rebuild

Read an immutable materialized journal/hold export from one REPEATABLE READ transaction. Independently fold entries using account normal sides and active reservation events. Compare every account, including zero-valued and missing accounts. Rebuild into a new generation table and compare before activating it. Initial activation is a maintenance operation with workspace writes/dispatch frozen and fresh equality checks; do not swap a stale background build under live writes. The old generation and mismatch report remain inspectable. Later online rebuild would need an explicit catch-up protocol and is outside R2.

## 3. Request identity, admission and concurrency

### L-ID1 — two identities

`operation_id` is a stable client-generated business UUID scoped to a workspace; it survives transport retries, callback delays and restore. `Idempotency-Key` is scoped by workspace, authenticated client ID, and route family (`purchase:v1`, `payout:v1`, `reversal:v1`). It identifies a transport submission, not a new business effect. The server derives provider identity as the literal tuple `(provider_namespace, workspace_id, operation_id)` with an unambiguous versioned encoding. It never creates a fresh provider key because the old request timed out.

Canonical request comparison uses validated domain fields only: fixed field order, UTF-8 encoding, normalized UUIDs, integer strings, normalized enum values; a documented version prefix is included. No timestamps generated by the server enter the client payload fingerprint. Store the canonical bytes as well as SHA-256 and compare bytes on a replay; a hash alone is not a proof of equality. Authentication secrets, trace IDs, display text and irrelevant HTTP header order are excluded. The independent test oracle uses literal expected normalized cases, not the same canonicalizer implementation.

The chosen local retention is the entire retained workspace history. There is no automatic expiry or reuse. An explicit destructive reset creates a new workspace/provider namespace and cannot share old keys. This differs from providers' finite idempotency windows [L-05](../01-REFERENCE-REGISTER.md#l-05).

| Submission | Required result |
|---|---|
| Same key + identical canonical payload | Replay identical persisted admission status and JSON body; replay header may differ |
| Same key + changed amount/account/operation ID | `409 IDEMPOTENCY_CONFLICT`, no mutation |
| New key + same operation ID + identical payload | Link new request key to the existing admission result; no new effect |
| New key + same operation ID + different payload | `409 BUSINESS_ID_CONFLICT`, no mutation |
| Valid operation rejected for insufficient funds | Persist `422 INSUFFICIENT_AVAILABLE`, result is stable even if later funding arrives; a genuinely new decision needs a new operation ID |
| Malformed JSON, failed authentication, invalid schema | Reject before admission; no request reservation |
| DB lock/pool timeout before definite commit | `503 RETRY_SAME_IDENTITY` with retry guidance; do not persist a fabricated business rejection |
| Connection loss during COMMIT | Outcome is unknown to caller; reconnect and query/retry the same identities, never assume rollback |

### L-ID2 — transactional admission algorithm

There are two execution locations. **Python owns the outer transaction, timeouts, commit and rollback; one typed `ledger_api` admission function owns all private reads/writes for the submission.** The SQL function never executes transaction-control commands and never commits a partial claim. The `BEGIN`/`COMMIT`/`ROLLBACK` labels below describe host behavior, not statements inside a PostgreSQL function.

```text
Python host:
  authenticate and validate payload; compute canonical bytes
  BEGIN READ COMMITTED on one checked-out connection
    SET LOCAL lock_timeout = bounded configured value
    SET LOCAL statement_timeout = bounded configured value
    SELECT typed ledger_api.submit_purchase/request_payout/reverse_purchase(...)
      -> receive persisted admission status/body, but do not send them yet
  COMMIT (including deferred constraint checks)
  send the returned admission status/body only after successful COMMIT acknowledgement
  on a typed function exception: ROLLBACK the entire transaction, then map the error

Inside the selected VOLATILE SECURITY DEFINER admission function:
  validate typed domain fields, principal scope and required memberships
  INSERT request_result(..., provisional operation reference)
    ON CONFLICT DO NOTHING
  if conflict:
    -- A concurrent inserter is waited on by uniqueness. This SELECT is a NEW
    -- READ COMMITTED command snapshot inside the VOLATILE function after commit.
    SELECT terminal request result
    re-authorize this principal's access to the referenced operation
    if canonical bytes differ: RAISE typed IDEMPOTENCY_CONFLICT exception
    RETURN the persisted status/body to the host, without transaction control
  INSERT operation(... admission=IN_PROGRESS) ON CONFLICT DO NOTHING
  SELECT operation FOR UPDATE
  if existing terminal operation:
    re-authorize this principal's access to this operation
    if canonical bytes differ: RAISE typed BUSINESS_ID_CONFLICT exception
    persist request replay link/result
    RETURN the persisted status/body to the host, without transaction control
  lock all affected existing account_balance rows one-by-one in ascending UUID
  validate owner/classes/currency/enabled/available, and result bounds
  if business rejection:
    set operation REJECTED and persisted 422 body
    complete request_result with same body
  else:
    call private posting/hold primitive
    persist admission ACCEPTED and immutable 201/202 body
    complete request_result with same body
  RETURN the persisted status/body to the host, without transaction control
```

Use explicit application SQLSTATEs for intentional aborts: `L1001` maps to `409 IDEMPOTENCY_CONFLICT`, `L1002` to `409 BUSINESS_ID_CONFLICT`, and `L1003` to the non-disclosing authorization error defined by the endpoint. These are proposed project codes, not PostgreSQL built-in conditions. A raised exception must escape the function and transaction context; the host maps it **after rollback**. Do not catch it inside the function and return an ordinary 409 row, because that could commit a provisional request claim. In contrast, insufficient available funds is a normal business result: the function writes and returns its terminal 422 result, and the host commits it. Do not translate every uniqueness or CHECK violation to a business conflict; unexpected constraints are implementation errors.

The explicit `VOLATILE` property matters for the separate statements after `INSERT ... ON CONFLICT DO NOTHING`: at READ COMMITTED, their lookups must obtain a fresh command snapshot after waiting for the winning transaction. Do not collapse the claim and replay lookup into a single statement whose earlier snapshot cannot see that winner. The concurrency experiment must exercise this exact function boundary, not a privileged Python implementation that bypasses it [L-04](../01-REFERENCE-REGISTER.md#l-04).

The selected schema uses a nonnull operation ID with a composite operation FK declared DEFERRABLE INITIALLY DEFERRED. Thus the request claim may precede the new operation within the same transaction. Deferred INSERT/UPDATE constraint triggers inspect the final request row and require nonnull valid HTTP status and JSON response body, and inspect the operation row to prohibit IN_PROGRESS at commit. These triggers use the protected function-owner pattern above. Once a request result is terminal, an immutability trigger rejects changing its payload/status/body/identity. Any identity conflict or authorization failure after creating a provisional claim rolls back the entire transaction before returning; no half-complete request result is committed. A crashed transaction leaves neither claim nor partial financial effect. The runtime must not catch a SQL error and continue in the aborted transaction; rollback first [L-08](../01-REFERENCE-REGISTER.md#l-08).

With contention, the caller may wait only up to the configured request bound; `503` is not proof the other transaction failed. For SQLSTATE `40P01`/`40001`, retry the **entire** transaction at most three times with jitter, retaining the same identities; exhaustion yields retryable 503. Never repeat only the final SQL statement. Other constraint errors are surfaced as implementation errors unless explicitly mapped to domain outcomes.

Lock ordering: request claim → business operation → its dispatch row, if present → affected accounts sorted by UUID. Existing multi-operation administrative actions sort operation IDs first. Dispatcher candidates are first selected without locks, then operation and intent rows are locked/revalidated in this order; do not hold an outbox row while waiting for an operation lock taken in the opposite order. Callback handlers never acquire request-key locks.

### L-ID3 — precise READ COMMITTED comparison

For a customer balance of 100 and two different debits of 80:

- `SELECT balance`, then both clients submit `UPDATE balance SET balance = 20` from stale application arithmetic: both may report success and the projection loses one debit; journal totals can disagree with it.
- `UPDATE balance SET balance = balance - 80` without a nonnegative guard: the row updates serialize but can produce −60. A correct CHECK would reject the second update, and all associated writes must roll back.
- `UPDATE ... SET balance = balance - 80 WHERE balance - reserved >= 80 RETURNING ...`: under PostgreSQL READ COMMITTED re-evaluation of an updated target row, one update can succeed and the other return zero rows. Multi-account journals still need atomic transactions and consistent lock ordering.
- Recommended `SELECT ... FOR UPDATE`, re-read available balance and post under that lock: one admitted purchase, one stable rejection, customer 20. The winner is not specified by request start order.

The alternative experiment compares conditional updates with the recommended locks using the same account topology, hot-key skew, invariant checks and failure traces. PostgreSQL's documented statement behavior is the source; our complete business result still requires real-component tests [L-04](../01-REFERENCE-REGISTER.md#l-04).

## 4. Public API contract

### L-API1 — examples

```http
POST /v1/workspaces/{workspace_id}/purchases
Authorization: Bearer <local-fixture-token>
Idempotency-Key: purchase-example-1
Content-Type: application/json

{"operation_id":"00000000-0000-4000-8000-000000000001",
 "customer_account_id":"00000000-0000-4000-8000-000000000101",
 "merchant_account_id":"00000000-0000-4000-8000-000000000201",
 "amount_minor":"80","currency":"PLN"}
```

201 persisted body:

```json
{"operation_id":"00000000-0000-4000-8000-000000000001","admission":"accepted",
 "journal_id":"00000000-0000-4000-8000-000000000301",
 "amount_minor":"80","currency":"PLN","effect":"purchase_posted","contract_version":1}
```

Do not put a mutable current balance in this immutable response. `GET /accounts/{id}` returns the current `booked_minor`, `reserved_minor`, `available_minor`, `version` and currency. `GET /operations/{operation_id}` returns admission and current financial/recovery state; it can differ from the original admission response without changing replay semantics.

`POST /payouts` accepts operation ID, merchant account, beneficiary synthetic ID, principal, quoted fee, currency and provider contract version. Its immutable 202 body states `accepted_for_processing`, payout ID and a status URL; it never claims the provider paid. `POST /purchases/{original}/reversal` takes a new operation ID and reason. `GET /reconciliation/runs/{id}` returns fixed versions and state; incomplete runs cannot be presented as finalized. `POST /operator-actions` and `POST /operator-actions/{id}/approve` use explicit evidence/action digests and expected versions; `POST .../execute` is idempotent on action ID.

All errors use `{error:{code,message,retryable,operation_id?,request_id}}`. Codes include `INVALID_REQUEST` 400/422, `UNAUTHENTICATED` 401, `NOT_FOUND` 404, `IDEMPOTENCY_CONFLICT` 409, `BUSINESS_ID_CONFLICT` 409, `STALE_EVIDENCE` 409, `INSUFFICIENT_AVAILABLE` 422, `CONTRADICTORY_PROVIDER_EVIDENCE` 409, `RESTORE_GAP` 503 and `RETRY_SAME_IDENTITY` 503. Error messages must not reveal another workspace's objects.

## 5. External payout contract

### L-EXT1 — state dimensions

| Dimension | States | Meaning |
|---|---|---|
| Money/hold | HELD → CONSUMED or RELEASED | Availability and whether the final local outflow is posted |
| Knowledge | NOT_SENT → UNKNOWN / ACCEPTED → SUCCEEDED / FAILED / CLOSED_ABSENT; any contradiction → CONTRADICTORY | What validated provider evidence supports |
| Transport | READY / MAY_HAVE_SENT / RESPONSE / TIMEOUT / CONNECTION_ERROR | Attempt observations; not a financial outcome |

Valid central combinations: HELD+NOT_SENT; HELD+UNKNOWN; HELD+ACCEPTED; CONSUMED+SUCCEEDED; RELEASED+FAILED or CLOSED_ABSENT. Local cancellation before any dispatch claim yields RELEASED+NOT_SENT with an immutable cancellation event. Contradiction preserves the last committed money state and freezes automation; it never changes a consumed hold to released.

### L-EXT2 — provider authority and retention

The provider process exposes:

- `PUT /v1/executions/{provider_key}`: normalized fixed request `(operation_id, merchant_id, beneficiary_id, principal, fee, gross, currency, contract_version)`. In one SQLite `BEGIN IMMEDIATE` transaction, compare an existing key or create exactly one accepted request. Effects and terminal result persist together. Same key/different payload returns conflict. A tombstoned key returns terminal `CLOSED_ABSENT` and can never execute.
- `GET /v1/executions/{key}`: optionally lagging lookup with `visibility_watermark`, `source_sequence`, payload digest and observation signature. `NOT_FOUND` is provisional even if the local clock has advanced.
- `POST /v1/executions/{key}/close-if-absent`: a strongly serialized authority transaction. If absent, insert permanent `CLOSED_ABSENT` tombstone bound to the exact request digest; if present, return its actual accepted/terminal state without deleting it. A pending accepted execution is not cancelled by absence logic.
- `GET /v1/history?after=...&watermark=...`: authoritative immutable execution/tombstone history and declared completeness up to a fixed provider sequence. Pagination preserves the same watermark.
- `GET /v1/statements/{business_date}/{version}`: independently generated settlement file, manifest, explicit group membership, fee rule version and source sequence bounds.

Provider keys and tombstones persist for the scenario namespace lifetime; reset requires a new namespace and cannot be used to recover old unknown effects. It may crash before commit, after commit before response, delay a read/callback, duplicate callbacks, reorder callbacks or emit deliberate contract-violating observations for tests. Proposed SQLite settings are `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, and a finite busy timeout on a local filesystem. Bootstrap must inspect the actually linked SQLite version and applicable known fixes; Python's version alone does not pin it. In particular, require SQLite 3.51.3 or later, or documented vendor/backport evidence for the WAL-reset fix (officially listed backports include 3.44.6 and 3.50.7), before enabling concurrent WAL connections [L-09](../01-REFERENCE-REGISTER.md#l-09). Otherwise stop with an actionable dependency error or explicitly use and test a rollback-journal variant. These settings are a simulator implementation choice, not a claim of remote banking durability. No external bank endpoint is configured.

The authority contract is intentionally stronger than an ordinary eventually consistent lookup. If that capability is disabled in an adversarial scenario, automatic non-execution proof is unavailable: preserve the hold and escalate. A complete statement showing no item is insufficient unless the provider contract explicitly proves that no later execution of that key remains possible.

### L-EXT3 — dispatch, observation and local resolution

Request-payout transaction: admit identity → lock merchant → check availability of gross → insert reservation+HELD event → increase reserved projection → insert payout and READY intent → persist 202 result → commit. No network I/O inside it.

Claim transaction: lock operation then intent, revalidate READY/due/lease, append attempt with `MAY_HAVE_SENT`, transition knowledge from NOT_SENT to UNKNOWN, change intent to IN_FLIGHT/OBSERVING, commit. Then make the provider request. A crash before the actual send still follows conservative recovery; an unnecessary lookup is safer than inventing non-execution.

Observation transaction: verify signature/source, scope, provider key, digest, gross/merchant/currency and sequence; lock operation/payout, then its intent, then merchant/clearing accounts in sorted order; insert immutable observation. Apply ordinary state progress only for a newer compatible sequence. A valid new terminal success consumes HELD and posts Dr merchant gross/Cr clearing gross once. A definitive failed/closed-absent result releases HELD with no outflow. Duplicate same results and stale nonterminal observations add no effect. Incompatible authenticated terminal states produce an incident even if one has an older sequence: terminal immutability is part of the provider contract. Same event ID/different bytes is also an incident. Mark intent done in the same transaction where compatible final resolution occurs.

Authentication uses a local test HMAC key with explicit key ID, signed payload bytes and comparison resistant to timing leakage. Persist event IDs for replay defense and bound message size. Signature freshness must allow explicitly delayed synthetic events, so reject unauthorized/replayed content by identity and digest rather than discarding legitimate late evidence solely because it is old. This is not a production provider integration.

Retry transport immediately at most five times with capped backoff and jitter; then poll/recover through a separate bounded work schedule. These are resource limits, **not** a deadline that proves failure. Operator escalation records the unresolved age/evidence; it cannot manufacture a payout cancellation. No new provider key is generated by a retry path.

## 6. Reconciliation and publication

### L-REC1 — inputs and deterministic matching

`business_manifest` is frozen before exercising the SUT: command IDs, owner/account mapping, exact amounts, expected domain outcomes where hand-computable, and source hashes. For randomized concurrent cases it contains permissible outcomes/invariants rather than a preselected winner. Provider statement files originate from provider records, not ledger export. The three evidence sets can each be wrong; disagreement is an investigation result, not automatic proof one side is authoritative for every fact.

Classify at least `MISSING_INTERNAL`, `UNEXPECTED_INTERNAL`, `DUPLICATE_EFFECT`, `WRONG_OWNER`, `WRONG_AMOUNT`, `CURRENCY_MISMATCH`, `MISSING_PROVIDER`, `UNEXPECTED_PROVIDER`, `TIMING_ONLY`, `FEE_MISMATCH`, `AMBIGUOUS_GROUP`, `INPUT_CONTRADICTION`, `CORRUPT_INPUT`. Compare expected owner before accepting equal totals. Provider payout principal+fee must equal local gross; internal purchases have no expected settlement row.

Group key is `(provider_namespace, settlement_batch_id, currency, merchant_id, fee_rule_version)` with explicit member keys. A batch summary may combine several payouts; supplied membership drives an exact sum. Multiple summaries for a split batch have distinct statement-row IDs and a shared group; duplicate member allocation is an error. An absent grouping reference yields ambiguity, not a subset-sum search for coincidentally matching amounts. The first algorithm is a hash join on IDs and group aggregation, expected O(n+m) time and O(unique keys) memory. Bounded external sort/merge is an alternative O(N log N) time with bounded RAM; compare when memory measurements justify it.

### L-REC2 — input and output versions

Capture journal/operation/hold export in one read-only REPEATABLE READ transaction and materialize its exact records to an immutable local file. A DB sequence alone is not a commit-consistent snapshot identifier. The run manifest contains schema version, workspace/provider namespace, business date and timezone, inclusive/exclusive cutoff definition, materialized ledger export hash, business manifest hash, provider statement version+hash, fee/matching rule versions, code commit, parser version and partition plan hash. Files are content-addressed bytes; publish via write → flush/fsync → rename within one filesystem → fsync directory → verify hash/length → insert referenced metadata. If that local filesystem guarantee is unavailable, fail the publication capability check. A DB transaction does not atomically write files.

An EOD run uses records with business-effective time in `[start_of_day, next_start_of_day)` in `Europe/Warsaw`, with UTC boundaries computed for that date, and records observable in the pinned exports. Late statements produce a new run for the same date. Do not reinterpret late receipt as contemporaneous business knowledge. Each output file includes the run/partition ID, exact input digest, schema and rule versions.

Publication flow: verify all source files → create STAGING run and expected partition rows → calculate deterministic results → write a private temporary result file → acquire the workspace's dedicated object-registry row lock → rename/flush/verify the final immutable object and register it **with a durable publisher pin** in that same locked DB transaction → submit the registered digest under the partition generation → replace the publisher pin with a durable accepted-result/run reference in the acceptance transaction → after every expected partition is accepted, validate coverage/no overlap/totals/file integrity → atomically mark COMPLETE and optionally CAS the “current run” pointer. A rejected attempt retains its pin until explicit cleanup; automatic expiry cannot make an in-flight publication collectible. The object-registry lock is distinct from financial account locks. Writers, reader-pin acquisition and GC use this same lock; when combined with run/partition locks, order is object registry → run → partition. A writer losing the DB connection/transaction aborts publication and cannot continue to register the file in an ambiguous old context. Readers pin a run ID. They never read STAGING output as an authoritative complete report. A superseding run does not overwrite earlier evidence.

### L-REC3 — authoritative fencing

```sql
BEGIN;
SELECT workspace_id FROM object_registry_guard
 WHERE workspace_id = :workspace FOR UPDATE;
SELECT state FROM recon_run WHERE id = :run FOR UPDATE;
-- Parent run lock serializes acceptance against cancellation/publication.
SELECT state, generation, lease_owner, lease_until
  FROM recon_partition
 WHERE run_id = :run AND partition_id = :part
 FOR UPDATE;
-- Reject unless RUNNING, generation=:token, owner=:worker,
-- lease_until > clock_timestamp(), and the parent run remains STAGING.
-- Validate result metadata/input digest and previously durable file integrity.
INSERT INTO accepted_partition_result(run_id,partition_id,generation,digest)
VALUES (:run,:part,:token,:digest);  -- unique(run_id, partition_id)
UPDATE recon_partition SET state='ACCEPTED', result_digest=:digest
 WHERE run_id=:run AND partition_id=:part AND generation=:token;
-- Add the durable run/result reference before releasing the publisher pin.
COMMIT;
```

Claim, acceptance, cancellation and final publication all acquire the parent run lock before partition locks, with partitions sorted when more than one is touched. Reassignment verifies expiry, increments generation and changes owner under these locks. No check-then-publish outside that transaction. Lease renewal uses the same owner/generation predicate. DB time determines leases; a worker's local clock does not. Parent publication also validates no failed/cancelled run state. Lease expiration and actual safety are distinguished: the token protects acceptance after reassignment, while the explicit expiration predicate enforces the chosen stricter contract even before reassignment. If a partition is already ACCEPTED, an authenticated retry with the exact accepted generation, owner, input manifest and result digest returns ALREADY_ACCEPTED without any rewrite; expiry does not invalidate an already accepted result. Any changed digest/token is rejected. A failed attempt leaves only an orphan file eligible for later GC, not a visible accepted result.

Files referenced by a retained run, release evidence or active reader pin are ineligible for garbage collection. Registered objects that have become unreferenced may be marked, then rechecked/deleted under the same object-registry lock after a documented grace interval. Registration and deletion cannot race because promotion/registration holds that lock through the DB commit. Unknown or unregistered files and private temporary files are cleaned only during a quiesced maintenance pass with all publishers stopped; grace time alone is not proof that a paused publisher is dead. A crash between rename and registration leaves an orphan, never a valid run reference. A missing/corrupt referenced file causes a reader error and an availability incident; never fall back to another run silently. Backup inventory includes all referenced bytes and pins. Hash equality establishes byte identity, not that the source facts were true.

## 7. Operator actions, restore and audit

### L-OPS1 — stale approval prevention

An action proposal binds action type, exact target IDs and amounts, reason, maker ID, immutable evidence manifest/run/difference IDs, proposal hash, expected operation/reservation/account versions and expiry. A checker with permission in that workspace must be a different principal. On execution, re-authenticate, lock the action and affected state, recheck proposal hash, maker/checker separation, current permissions, evidence validity and expected versions, then use a normal idempotent operation keyed by action ID. Stale evidence returns 409 and requires a new proposal/review; approval is not a blanket capability to act on future state. Read-only investigations do not need approval. No operator may “release unknown payout” without the external non-execution prerequisite.

### L-OPS2 — restore safety

Every restore starts in RESTORE mode with dispatch physically disabled by process configuration as well as DB mode, so an old backup cannot reset the freeze flag to NORMAL. Capture the independent provider's fixed history watermark and check all restored unresolved and recent completed payouts. Compare command/request archives, local identities and provider records. Missing local success may be reconstructed only from retained business instruction and authoritative external evidence through a reviewed recovery entry point; no external resend occurs. If evidence is incomplete, preserve a gap list and freeze the affected workspace. Restoring a backup cannot recover lost instructions or keys from nothing.

Preserve historical request results when recovering; an old reply body that said “accepted” remains accepted, while GET state reflects recovered execution. A restored idempotency index with missing newer keys is insufficient: recover keys from retained external command records or keep that namespace non-writable. Backups and provider history require explicit retention horizons. This design makes no RPO/RTO promise until drills measure data loss and recovery time.

## 8. Worked golden traces

### T1 — concurrent independent purchases

Initial: clearing D100; customer C100; merchants M1/M2 zero. A submits purchase 80 to M1; B submits purchase 80 to M2 with different operation IDs/keys. With row locking, either A or B wins; suppose A wins. Persisted: A ACCEPTED/201 with journal Dr customer 80/Cr M1 80; B REJECTED/422 with no journal. Balances customer 20, M1 80, M2 0, clearing 100, all reservations 0. If B retries, still 422. The verifier accepts the symmetric M2 winner too; “first HTTP request wins” is not claimed.

### T2 — commit then lost reply

Same purchase commits A's operation, request result, two entries and both projections. Failpoint kills the API before sending 201. The client observes connection loss. A retry with the same identities returns the exact stored 201 body, same journal ID and no second pair of entries. With a new key but the same operation ID/payload the effect is also unchanged. Same key with amount 81 returns 409.

### T3 — provider succeeds before dispatcher crash

Starting after T1, merchant M1 has 80. Payout principal 60 + fee 2 holds 62: booked 80 / reserved 62 / available 18, knowledge NOT_SENT. Dispatcher commits MAY_HAVE_SENT and knowledge UNKNOWN, then sends; provider persists success and gross outflow 62, then the dispatcher is killed before local observation. Local state stays HELD+UNKNOWN; available remains 18. A lagging GET says NOT_FOUND: unchanged. An authoritative success observation later atomically posts Dr M1 62 / Cr clearing 62 and consumes the hold. Final M1 booked 18 / reserved 0 / available 18, clearing 38, customer 20; provider principal 60 / fee 2. Duplicate callback changes no money.

### T4 — definitive absence and a delayed submit

Hold 62 exists, a dispatch attempt is uncertain, and no provider execution was actually committed. `close-if-absent` inserts a CLOSED_ABSENT tombstone. Local observation releases 62: booked 80 / reserved 0 / available 80, no outflow journal. A delayed original PUT arrives afterward; the provider returns the tombstone and cannot execute. If the PUT won the serialization race first, closure instead returns that execution and the hold remains until its terminal outcome. A mere NOT_FOUND could not establish this safety.

### T5 — total balances but merchant is wrong

Frozen command says customer → M1 80. Deliberately defective journal is Dr customer 80/Cr M2 80. Debits equal credits and the total liability is 100, so journal balance checks alone pass. Independent owner comparison emits WRONG_OWNER with command ID, expected M1 and observed M2; reconciliation cannot auto-correct when M2 may already have spent funds. The mutation must be caught even if operation metadata was corrupted consistently with the wrong journal.

### T6 — stale worker and stale approval

Partition generation 7 belongs to worker A. It expires; worker B claims generation 8 and publishes a verified digest. A then submits generation 7: authoritative transaction rejects it; the COMPLETE run contains only generation 8. Separately, maker proposes a correction against run R1 / evidence hash H1 / version 4 and checker approves. Revised statement creates R2/H2 or state reaches version 5. Execute rejects STALE_EVIDENCE; no new journal. A new proposal and review are required.

### T7 — old local backup, external state ahead

Restore a backup taken before T3's local admission while leaving provider success intact. API/dispatch remain disabled. Provider history identifies the operation; an independently retained command manifest provides original customer/merchant/key/payload. Recovery reconstructs missing local records under the original identity and records a recovery audit, with **zero** provider PUTs. If that independent manifest is absent, the expected outcome is RESTORE_GAP and a frozen workspace, not a guessed new payment or an invented successful recovery.
