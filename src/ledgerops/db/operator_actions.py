"""One reviewed action consumes retained terminal evidence after a local repair.

All writes use workspace -> registry -> auth -> provider namespace -> action ->
payout -> hold -> sorted balances. No provider request is sent from this module.
The database login is an explicit trusted operator boundary, never an HTTP input.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from psycopg.types.json import Jsonb

from ledgerops.authentication import (
    AuthenticatedCredential,
    PermissionDenied,
    authorized_snapshot,
)
from ledgerops.db.admission import AdmissionError, InvariantViolation, NotAuthorized
from ledgerops.db.payout_dispatch import _idle, _lock_work, _retry, _transaction
from ledgerops.db.payout_finance import LocalSettlementBlocked, settle
from ledgerops.db.payout_resolution import TERMINAL, _contradiction
from ledgerops.operator_evidence import assert_target_current, load_action_evidence
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust, verify_observation
from ledgerops.reconciliation.publication import ObjectStore, PublicationError, _registry

ACTION_TYPE = "REPROCESS_TERMINAL_LOCAL_BLOCKER"
MAX_ATTEMPTS = 100
MAX_PROPOSAL_BYTES = 512 * 1024
MAX_REVIEW_INCIDENTS = 100


@dataclass(frozen=True, slots=True)
class ActionResult:
    http_status: int
    body: dict
    replayed: bool = False


class ActionRejected(AdmissionError):
    def __init__(self, code, *, status=409):
        self.code, self.http_status = code, status
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _uuid(value):
    if type(value) is not UUID:
        raise TypeError("Typed UUID required")
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Lowercase SHA256 required")
    return bytes.fromhex(value)


def _configuration(conn, credential, store, trust):
    _idle(conn)
    if type(credential) is not AuthenticatedCredential:
        raise TypeError("Database-derived authenticated credential required")
    if type(store) is not ObjectStore or type(trust) is not ProviderTrust:
        raise TypeError("Configured evidence store and provider trust required")
    return credential.principal.workspace_id.value


def _guard(cur, credential, scope):
    workspace = credential.principal.workspace_id.value
    # This lock also serializes an unrelated date's publication. A proposal is
    # tied to the actual current workspace evidence epoch, not only file hashes.
    try:
        epoch = _registry(cur, workspace)
    except PublicationError as exc:
        raise ActionRejected(exc.code) from exc
    stamp = credential.authorization_guard(cur, credential.principal, scope)
    return epoch, stamp


def _namespace(cur, workspace, payout_id, trust):
    row = cur.execute(
        "SELECT provider_namespace FROM ledger.payout WHERE workspace_id=%s AND operation_id=%s",
        (workspace, payout_id),
    ).fetchone()
    if row is None:
        raise NotAuthorized()
    if str(row["provider_namespace"]) != str(trust.namespace):
        raise ActionRejected("PROVIDER_TRUST_MISMATCH", status=422)
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"ledgerops:provider:{row['provider_namespace']}",),
    )
    return row["provider_namespace"]


def _action(cur, workspace, action_id):
    row = cur.execute(
        "SELECT * FROM ledger.operator_action WHERE workspace_id=%s AND action_id=%s FOR UPDATE",
        (workspace, action_id),
    ).fetchone()
    if row is None:
        raise NotAuthorized()
    return row


def _identity(credential):
    return credential.principal.actor_id.value, credential.principal.client_id.value


def _person_current(cur, workspace, actor, client, membership_version, client_version, scope):
    # Human approval survives credential rotation. Stable membership/client
    # identity and their current versions are what the approval binds.
    row = cur.execute(
        "SELECT m.enabled AS member_enabled,m.scopes,m.version AS membership_version,"
        "c.enabled AS client_enabled,c.version AS client_version,c.actor_id "
        "FROM ledger.auth_membership m JOIN ledger.auth_client c "
        "ON c.workspace_id=m.workspace_id AND c.actor_id=m.actor_id "
        "WHERE m.workspace_id=%s AND m.actor_id=%s AND c.client_id=%s",
        (workspace, actor, client),
    ).fetchone()
    if (
        row is None
        or not row["member_enabled"]
        or not row["client_enabled"]
        or scope not in row["scopes"]
        or row["membership_version"] != membership_version
        or row["client_version"] != client_version
    ):
        raise ActionRejected("STALE_REVIEWER_AUTHORIZATION")


def _unresolved(cur, workspace, payout_id):
    # No LIMIT: the bounded UI summary is never clearing authority. Related
    # incidents can belong to another workspace and can never be disposed here.
    return cur.execute(
        "SELECT i.* FROM ledger.unresolved_payout_incident i WHERE "
        "(i.workspace_id=%s AND i.payout_id=%s) OR EXISTS "
        "(SELECT 1 FROM ledger.provider_observation o WHERE "
        "o.observation_id=i.related_observation_id AND o.workspace_id=%s AND o.payout_id=%s) "
        "ORDER BY i.workspace_id,i.incident_id",
        (workspace, payout_id, workspace, payout_id),
    ).fetchall()


def _target(cur, workspace, payout_id, observation_id, evidence_run_id, store, trust):
    namespace = cur.execute(
        "SELECT provider_namespace FROM ledger.payout WHERE workspace_id=%s AND operation_id=%s",
        (workspace, payout_id),
    ).fetchone()["provider_namespace"]
    payout, _ = _lock_work(cur, workspace, payout_id)
    if payout is None:
        raise NotAuthorized()
    row = cur.execute(
        "SELECT * FROM ledger.provider_observation WHERE workspace_id=%s AND payout_id=%s "
        "AND observation_id=%s",
        (workspace, payout_id, observation_id),
    ).fetchone()
    if row is None:
        raise NotAuthorized()
    if row["kind"] != "execution" or row["state"] not in TERMINAL:
        raise ActionRejected("TERMINAL_OBSERVATION_REQUIRED", status=422)
    try:
        observation = verify_observation(
            row["envelope"],
            trust=trust,
            provider_key=payout["provider_key"],
            request_bytes=payout["provider_request"],
        )
    except InvalidObservation as exc:
        raise ActionRejected("SOURCE_AUTHENTICATION_FAILED", status=422) from exc
    if (
        observation.state != row["state"]
        or observation.source_sequence != row["source_sequence"]
        or hashlib.sha256(row["envelope"]).digest() != row["envelope_sha256"]
    ):
        raise InvariantViolation("Retained observation columns disagree with signed source")
    try:
        evidence = load_action_evidence(cur, workspace, namespace, evidence_run_id, store)
        target = assert_target_current(cur, evidence, payout_id)
    except PublicationError as exc:
        raise ActionRejected(exc.code) from exc
    observations = cur.execute(
        "SELECT * FROM ledger.provider_observation WHERE workspace_id=%s AND payout_id=%s "
        "ORDER BY observation_id",
        (workspace, payout_id),
    ).fetchall()
    # Check every retained fact, including a related collision arriving after
    # the old reconciliation export. Namespace serialization makes this decision stable.
    if any(_contradiction(cur, fact)[0] is not None for fact in observations):
        raise ActionRejected("PROVIDER_CONTRADICTION", status=422)
    incidents = _unresolved(cur, workspace, payout_id)
    if not incidents or any(
        incident["category"] != "LOCAL_BLOCKER"
        or incident["workspace_id"] != workspace
        or incident["payout_id"] != payout_id
        or incident["observation_id"] != observation_id
        for incident in incidents
    ):
        raise ActionRejected("EXACT_LOCAL_BLOCKERS_REQUIRED", status=422)
    if len(incidents) > MAX_REVIEW_INCIDENTS:
        raise ActionRejected("INCIDENT_REVIEW_CAPACITY", status=422)
    if cur.execute(
        "SELECT 1 FROM ledger.payout_resolution WHERE workspace_id=%s AND payout_id=%s",
        (workspace, payout_id),
    ).fetchone():
        raise ActionRejected("PAYOUT_ALREADY_RESOLVED", status=422)
    facts = {
        "target": target,
        "evidence": {
            "evidence_epoch": evidence.evidence_epoch,
            "result_sha256": evidence.result_sha256,
            "ledger_data_sha256": evidence.ledger_data_sha256,
        },
        "observation": {
            "observation_id": str(observation_id),
            "envelope_sha256": row["envelope_sha256"].hex(),
            "state": row["state"],
        },
        "observations": [
            {
                "observation_id": str(fact["observation_id"]),
                "envelope_sha256": fact["envelope_sha256"].hex(),
            }
            for fact in observations
        ],
        "incident_ids": sorted(str(incident["incident_id"]) for incident in incidents),
    }
    return payout, row, facts


def _proposal_result(row, replayed=False):
    return ActionResult(
        200 if replayed else 201,
        {
            "action_id": str(row["action_id"]),
            "proposal_sha256": row["proposal_sha256"].hex(),
            "proposal": json.loads(row["proposal"]),
        },
        replayed,
    )


def propose_action(
    conn,
    credential,
    *,
    action_id,
    payout_id,
    observation_id,
    evidence_run_id,
    reason,
    ttl_seconds,
    store,
    trust,
):
    workspace = _configuration(conn, credential, store, trust)
    action_id, payout_id, observation_id, evidence_run_id = map(
        _uuid, (action_id, payout_id, observation_id, evidence_run_id)
    )
    if type(reason) is not str or "\0" in reason or not 1 <= len(reason.encode()) <= 256:
        raise ValueError("Bounded review reason required")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
        raise ValueError("Review TTL from1second to1hour required")
    intent = _canonical(
        {
            "action_type": ACTION_TYPE,
            "payout_id": str(payout_id),
            "observation_id": str(observation_id),
            "evidence_run_id": str(evidence_run_id),
            "reason": reason,
            "ttl_seconds": ttl_seconds,
        }
    )
    actor, client = _identity(credential)

    def once():
        with _transaction(conn, workspace) as cur:
            _, stamp = _guard(cur, credential, "approval:propose")
            _namespace(cur, workspace, payout_id, trust)
            previous = cur.execute(
                "SELECT * FROM ledger.operator_action WHERE workspace_id=%s AND action_id=%s "
                "FOR UPDATE",
                (workspace, action_id),
            ).fetchone()
            if previous is not None:
                if (
                    previous["intent"],
                    previous["maker_actor_id"],
                    previous["maker_client_id"],
                ) != (
                    intent,
                    actor,
                    client,
                ):
                    raise ActionRejected("ACTION_IDENTITY_CONFLICT")
                return _proposal_result(previous, True)
            _, _, facts = _target(
                cur, workspace, payout_id, observation_id, evidence_run_id, store, trust
            )
            now = cur.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            expires = now + timedelta(seconds=ttl_seconds)
            proposal = {
                "schema_version": 1,
                "workspace_id": str(workspace),
                "action_id": str(action_id),
                **json.loads(intent),
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "maker": {
                    "actor_id": str(actor),
                    "client_id": str(client),
                    "membership_version": stamp["membership_version"],
                    "client_version": stamp["client_version"],
                },
                "auth_generation": stamp["generation"],
                **facts,
            }
            raw_proposal = _canonical(proposal)
            if len(raw_proposal) > MAX_PROPOSAL_BYTES:
                raise ActionRejected("ACTION_PROPOSAL_CAPACITY", status=422)
            row = cur.execute(
                "INSERT INTO ledger.operator_action(workspace_id,action_id,action_type,payout_id,"
                "observation_id,evidence_run_id,intent,proposal,maker_actor_id,maker_client_id,"
                "maker_membership_version,maker_client_version,auth_generation,"
                "expires_at,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    workspace,
                    action_id,
                    ACTION_TYPE,
                    payout_id,
                    observation_id,
                    evidence_run_id,
                    intent,
                    raw_proposal,
                    actor,
                    client,
                    stamp["membership_version"],
                    stamp["client_version"],
                    UUID(stamp["generation"]),
                    expires,
                    now,
                ),
            ).fetchone()
            return _proposal_result(row)

    return _retry(conn, once)


def _review_current(cur, action, stamp, proposal_hash, store, trust):
    if action["proposal_sha256"] != proposal_hash:
        raise ActionRejected("PROPOSAL_HASH_CONFLICT")
    if action["auth_generation"] != UUID(stamp["generation"]):
        raise ActionRejected("STALE_RESTORE_GENERATION")
    now = cur.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    if now >= action["expires_at"]:
        raise ActionRejected("ACTION_EXPIRED")
    _person_current(
        cur,
        action["workspace_id"],
        action["maker_actor_id"],
        action["maker_client_id"],
        action["maker_membership_version"],
        action["maker_client_version"],
        "approval:propose",
    )
    payout, observation, facts = _target(
        cur,
        action["workspace_id"],
        action["payout_id"],
        action["observation_id"],
        action["evidence_run_id"],
        store,
        trust,
    )
    proposal = json.loads(action["proposal"])
    if any(proposal[name] != value for name, value in facts.items()):
        raise ActionRejected("STALE_ACTION_EVIDENCE")
    if cur.execute("SELECT clock_timestamp() AS now").fetchone()["now"] >= action["expires_at"]:
        raise ActionRejected("ACTION_EXPIRED")
    return payout, observation, proposal


def _approval_result(row, replayed=False):
    return ActionResult(
        200 if replayed else 201,
        {
            "action_id": str(row["action_id"]),
            "proposal_sha256": row["proposal_sha256"].hex(),
            "checker_actor_id": str(row["checker_actor_id"]),
            "checker_client_id": str(row["checker_client_id"]),
            "approved_at": row["approved_at"].isoformat(),
        },
        replayed,
    )


def approve_action(conn, credential, *, action_id, proposal_sha256, store, trust):
    workspace = _configuration(conn, credential, store, trust)
    action_id, proposal_hash = _uuid(action_id), _hash(proposal_sha256)
    actor, client = _identity(credential)

    def once():
        with _transaction(conn, workspace) as cur:
            _, stamp = _guard(cur, credential, "approval:approve")
            # Immutable target identity can be read before the namespace lock.
            known = cur.execute(
                "SELECT payout_id FROM ledger.operator_action "
                "WHERE workspace_id=%s AND action_id=%s",
                (workspace, action_id),
            ).fetchone()
            if known is None:
                raise NotAuthorized()
            _namespace(cur, workspace, known["payout_id"], trust)
            action = _action(cur, workspace, action_id)
            if actor == action["maker_actor_id"]:
                raise PermissionDenied()
            _review_current(cur, action, stamp, proposal_hash, store, trust)
            previous = cur.execute(
                "SELECT * FROM ledger.operator_approval WHERE workspace_id=%s AND action_id=%s",
                (workspace, action_id),
            ).fetchone()
            if previous is not None:
                if (previous["checker_actor_id"], previous["checker_client_id"]) != (actor, client):
                    raise ActionRejected("APPROVAL_IDENTITY_CONFLICT")
                return _approval_result(previous, True)
            row = cur.execute(
                "INSERT INTO ledger.operator_approval(workspace_id,action_id,proposal_sha256,"
                "checker_actor_id,checker_client_id,checker_membership_version,"
                "checker_client_version) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    workspace,
                    action_id,
                    proposal_hash,
                    actor,
                    client,
                    stamp["membership_version"],
                    stamp["client_version"],
                ),
            ).fetchone()
            return _approval_result(row)

    return _retry(conn, once)


def _attempt(cur, workspace, action_id, execution_id, actor, client, proposal_hash):
    row = cur.execute(
        "SELECT * FROM ledger.operator_attempt WHERE workspace_id=%s AND action_id=%s "
        "AND execution_id=%s",
        (workspace, action_id, execution_id),
    ).fetchone()
    if row is not None and (
        row["executor_actor_id"],
        row["executor_client_id"],
        row["proposal_sha256"],
    ) != (actor, client, proposal_hash):
        raise ActionRejected("EXECUTION_IDENTITY_CONFLICT")
    return row


def _save_attempt(cur, workspace, action_id, execution_id, actor, client, proposal_hash, result):
    count = cur.execute(
        "SELECT count(*) AS count FROM ledger.operator_attempt "
        "WHERE workspace_id=%s AND action_id=%s",
        (workspace, action_id),
    ).fetchone()["count"]
    if count >= MAX_ATTEMPTS:
        raise ActionRejected("ACTION_ATTEMPT_LIMIT", status=422)
    cur.execute(
        "INSERT INTO ledger.operator_attempt(workspace_id,action_id,execution_id,proposal_sha256,"
        "executor_actor_id,executor_client_id,http_status,response_body) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            workspace,
            action_id,
            execution_id,
            proposal_hash,
            actor,
            client,
            result.http_status,
            Jsonb(result.body),
        ),
    )


def _stored(row):
    return ActionResult(row["http_status"], row["response_body"], True)


def _execution_open(cur, credential, workspace, action_id, execution_id, proposal_hash, trust):
    _, stamp = _guard(cur, credential, "approval:execute")
    known = cur.execute(
        "SELECT payout_id FROM ledger.operator_action WHERE workspace_id=%s AND action_id=%s",
        (workspace, action_id),
    ).fetchone()
    if known is None:
        raise NotAuthorized()
    _namespace(cur, workspace, known["payout_id"], trust)
    action = _action(cur, workspace, action_id)
    actor, client = _identity(credential)
    previous = _attempt(cur, workspace, action_id, execution_id, actor, client, proposal_hash)
    return stamp, action, previous


def execute_action(conn, credential, *, action_id, execution_id, proposal_sha256, store, trust):
    workspace = _configuration(conn, credential, store, trust)
    action_id, execution_id, proposal_hash = (
        _uuid(action_id),
        _uuid(execution_id),
        _hash(proposal_sha256),
    )
    actor, client = _identity(credential)

    def once():
        with _transaction(conn, workspace) as cur:
            stamp, action, previous = _execution_open(
                cur, credential, workspace, action_id, execution_id, proposal_hash, trust
            )
            if previous is not None:
                return _stored(previous)
            executed = cur.execute(
                "SELECT * FROM ledger.operator_execution WHERE workspace_id=%s AND action_id=%s",
                (workspace, action_id),
            ).fetchone()
            if action["proposal_sha256"] != proposal_hash:
                raise ActionRejected("PROPOSAL_HASH_CONFLICT")
            if executed is not None:
                result = _stored(executed)
                _save_attempt(
                    cur, workspace, action_id, execution_id, actor, client, proposal_hash, result
                )
                return result
            approval = cur.execute(
                "SELECT * FROM ledger.operator_approval WHERE workspace_id=%s AND action_id=%s",
                (workspace, action_id),
            ).fetchone()
            if approval is None:
                raise ActionRejected("DISTINCT_APPROVAL_REQUIRED", status=422)
            _person_current(
                cur,
                workspace,
                approval["checker_actor_id"],
                approval["checker_client_id"],
                approval["checker_membership_version"],
                approval["checker_client_version"],
                "approval:approve",
            )
            payout, observation, proposal = _review_current(
                cur, action, stamp, proposal_hash, store, trust
            )
            now = cur.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            settle(cur, payout, observation, now)
            resolution = cur.execute(
                "SELECT * FROM ledger.payout_resolution WHERE workspace_id=%s AND payout_id=%s",
                (workspace, payout["operation_id"]),
            ).fetchone()
            result = ActionResult(
                200,
                {
                    "action_id": str(action_id),
                    "execution_id": str(execution_id),
                    "payout_id": str(payout["operation_id"]),
                    "observation_id": str(observation["observation_id"]),
                    "proposal_sha256": proposal_sha256,
                    "state": resolution["state"],
                    "journal_id": None
                    if resolution["journal_id"] is None
                    else str(resolution["journal_id"]),
                    "resolved_incident_ids": proposal["incident_ids"],
                },
            )
            cur.execute(
                "INSERT INTO ledger.operator_execution(workspace_id,action_id,execution_id,"
                "proposal_sha256,executor_actor_id,executor_client_id,observation_id,state,journal_id,"
                "http_status,response_body,executed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    workspace,
                    action_id,
                    execution_id,
                    proposal_hash,
                    actor,
                    client,
                    observation["observation_id"],
                    resolution["state"],
                    resolution["journal_id"],
                    result.http_status,
                    Jsonb(result.body),
                    now,
                ),
            )
            for incident_id in proposal["incident_ids"]:
                cur.execute(
                    "INSERT INTO ledger.payout_incident_resolution("
                    "workspace_id,incident_id,payout_id,"
                    "action_id,observation_id,resolved_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        workspace,
                        UUID(incident_id),
                        payout["operation_id"],
                        action_id,
                        observation["observation_id"],
                        now,
                    ),
                )
            _save_attempt(
                cur, workspace, action_id, execution_id, actor, client, proposal_hash, result
            )
            return result

    try:
        return _retry(conn, once)
    except LocalSettlementBlocked as exc:
        rejected = ActionRejected(exc.code, status=422)
    except ActionRejected as exc:
        rejected = exc
    # Only definite business rejections reach here after rollback. SQL/transport
    # errors, especially unknown COMMIT, propagate without inventing a failure.
    result = ActionResult(
        rejected.http_status, {"code": rejected.code, "action_id": str(action_id)}
    )

    def retain_rejection():
        with _transaction(conn, workspace) as cur:
            _, action, previous = _execution_open(
                cur, credential, workspace, action_id, execution_id, proposal_hash, trust
            )
            if previous is not None:
                return _stored(previous)
            executed = cur.execute(
                "SELECT * FROM ledger.operator_execution WHERE workspace_id=%s AND action_id=%s",
                (workspace, action_id),
            ).fetchone()
            # A concurrent successful executor may have won between the failed
            # transaction and this audit transaction. Report that actual effect.
            outcome = (
                _stored(executed)
                if executed is not None and action["proposal_sha256"] == proposal_hash
                else result
            )
            _save_attempt(
                cur, workspace, action_id, execution_id, actor, client, proposal_hash, outcome
            )
            return outcome

    return _retry(conn, retain_rejection)


def action_state(conn, credential, *, action_id):
    """Bounded metadata/audit read under the existing current-auth RR protocol."""
    _idle(conn)
    if type(credential) is not AuthenticatedCredential:
        raise TypeError("Database-derived authenticated credential required")
    action_id = _uuid(action_id)
    workspace = credential.principal.workspace_id.value
    with authorized_snapshot(conn, credential, "evidence:read") as cur:
        row = cur.execute(
            "SELECT * FROM ledger.operator_action WHERE workspace_id=%s AND action_id=%s",
            (workspace, action_id),
        ).fetchone()
        if row is None:
            raise NotAuthorized()
        result = _proposal_result(row, True).body
        approval = cur.execute(
            "SELECT * FROM ledger.operator_approval WHERE workspace_id=%s AND action_id=%s",
            (workspace, action_id),
        ).fetchone()
        result["approval"] = None if approval is None else _approval_result(approval).body
        effect = cur.execute(
            "SELECT response_body FROM ledger.operator_execution "
            "WHERE workspace_id=%s AND action_id=%s",
            (workspace, action_id),
        ).fetchone()
        result["execution"] = None if effect is None else effect["response_body"]
        attempts = cur.execute(
            "SELECT execution_id,executor_actor_id,executor_client_id,http_status,"
            "response_body,recorded_at "
            "FROM ledger.operator_attempt WHERE workspace_id=%s AND action_id=%s ORDER BY "
            "recorded_at DESC,execution_id DESC LIMIT 21",
            (workspace, action_id),
        ).fetchall()
        result["attempts"] = [
            {
                key: value.isoformat()
                if hasattr(value, "isoformat")
                else str(value)
                if type(value) is UUID
                else value
                for key, value in attempt.items()
            }
            for attempt in attempts[:20]
        ]
        result["attempts_have_more"] = len(attempts) > 20
        return result
