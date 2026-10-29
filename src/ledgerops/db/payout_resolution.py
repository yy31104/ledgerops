"""One authenticated observation path for callbacks, responses and recovery history."""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from ledgerops.db.admission import InvariantViolation, NotAuthorized
from ledgerops.db.payout_admission import utc_instant, utc_now
from ledgerops.db.payout_dispatch import _identity, _idle, _lock_work, _retry, _transaction
from ledgerops.db.payout_finance import LocalSettlementBlocked, advance_knowledge, settle
from ledgerops.domain import OperationId, WorkspaceId
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust, verify_observation

MAX_OBSERVATIONS = 1000
MAX_QUARANTINE = 100
CHANNELS = {"PUT", "LOOKUP", "CLOSE", "CALLBACK", "HISTORY"}
TERMINAL = {"SUCCEEDED", "FAILED", "CLOSED_ABSENT"}


@dataclass(frozen=True, slots=True)
class ObservationResult:
    status: str
    observation_id: UUID | None
    state: str | None
    code: str | None = None


def blocking_incidents(cur, workspace, payout_id):
    """Direct or cross-scope collision evidence; never lock or update another payout."""
    return cur.execute(
        "SELECT category,code FROM ledger.payout_incident WHERE workspace_id=%s AND payout_id=%s "
        "UNION SELECT i.category,i.code FROM ledger.payout_incident i JOIN "
        "ledger.provider_observation o "
        "ON o.observation_id=i.related_observation_id WHERE o.workspace_id=%s AND o.payout_id=%s "
        "ORDER BY category,code LIMIT 32",
        (workspace, payout_id, workspace, payout_id),
    ).fetchall()


def _stop(cur, workspace, payout_id):
    cur.execute(
        "UPDATE ledger.dispatch_intent SET state='BLOCKED' WHERE workspace_id=%s AND payout_id=%s "
        "AND state NOT IN ('DONE','BLOCKED')",
        (workspace, payout_id),
    )


def _incident(cur, payout, category, code, observation=None, related=None, incoming_digest=None):
    own_id = None if observation is None else observation["observation_id"]
    related_id = None if related is None else related["observation_id"]
    digest = incoming_digest if related is None else related["envelope_sha256"]
    params = (payout["workspace_id"], payout["operation_id"])
    found = cur.execute(
        "SELECT 1 FROM ledger.payout_incident WHERE workspace_id=%s AND payout_id=%s AND code=%s "
        "AND observation_id IS NOT DISTINCT FROM %s AND related_observation_id IS NOT DISTINCT "
        "FROM %s",
        (*params, code, own_id, related_id),
    ).fetchone()
    if found is None:
        cur.execute(
            "INSERT INTO ledger.payout_incident (workspace_id,incident_id,payout_id,category,code,"
            "observation_id,related_observation_id,related_payload_sha256) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s)",
            (params[0], uuid4(), params[1], category, code, own_id, related_id, digest),
        )
    _stop(cur, *params)


def _retain(cur, payout, observation, now):
    envelope = observation.signed_bytes
    previous = cur.execute(
        "SELECT * FROM ledger.provider_observation WHERE provider_namespace=%s AND event_id=%s "
        "AND envelope_sha256=%s AND envelope=%s LIMIT 1",
        (
            payout["provider_namespace"],
            UUID(observation.event_id),
            hashlib.sha256(envelope).digest(),
            envelope,
        ),
    ).fetchone()
    if previous is not None:
        if (previous["workspace_id"], previous["payout_id"]) != (
            payout["workspace_id"],
            payout["operation_id"],
        ):
            raise InvariantViolation("Identical provider event has inconsistent local scope")
        return previous, False
    count = cur.execute(
        "SELECT count(*) AS count FROM ledger.provider_observation WHERE workspace_id=%s AND "
        "payout_id=%s",
        (payout["workspace_id"], payout["operation_id"]),
    ).fetchone()["count"]
    if count >= MAX_OBSERVATIONS:
        _incident(
            cur,
            payout,
            "EVIDENCE_LIMIT",
            "EVIDENCE_CAPACITY",
            incoming_digest=hashlib.sha256(envelope).digest(),
        )
        return None, False
    row = cur.execute(
        "INSERT INTO ledger.provider_observation "
        "(observation_id,workspace_id,payout_id,provider_namespace,"
        "event_id,kind,state,source_sequence,key_id,signed_payload,signature,"
        "envelope,envelope_sha256,received_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            uuid4(),
            payout["workspace_id"],
            payout["operation_id"],
            payout["provider_namespace"],
            UUID(observation.event_id),
            observation.kind,
            observation.state,
            observation.source_sequence,
            observation.key_id,
            observation.payload,
            observation.signature,
            envelope,
            hashlib.sha256(envelope).digest(),
            now,
        ),
    ).fetchone()
    return row, True


def _receipt(cur, row, channel, attempt_id, now):
    cur.execute(
        "INSERT INTO ledger.observation_receipt "
        "(workspace_id,receipt_id,payout_id,observation_id,channel,"
        "transport_attempt_id,received_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (observation_id,channel) DO NOTHING",
        (
            row["workspace_id"],
            uuid4(),
            row["payout_id"],
            row["observation_id"],
            channel,
            attempt_id,
            now,
        ),
    )


def _contradiction(cur, row):
    other = cur.execute(
        "SELECT observation_id,envelope_sha256,state,source_sequence FROM "
        "ledger.provider_observation WHERE provider_namespace=%s AND event_id=%s "
        "AND observation_id<>%s ORDER BY received_at,observation_id LIMIT 1",
        (row["provider_namespace"], row["event_id"], row["observation_id"]),
    ).fetchone()
    if other is not None:
        return "EVENT_ID_REUSED", other
    if row["kind"] != "execution":
        return None, None
    other = cur.execute(
        "SELECT observation_id,envelope_sha256,state,source_sequence FROM "
        "ledger.provider_observation WHERE provider_namespace=%s AND kind='execution' "
        "AND source_sequence=%s AND observation_id<>%s ORDER BY received_at,observation_id LIMIT 1",
        (row["provider_namespace"], row["source_sequence"], row["observation_id"]),
    ).fetchone()
    if other is not None:
        return "SEQUENCE_REUSED", other
    others = cur.execute(
        "SELECT observation_id,envelope_sha256,state,source_sequence FROM "
        "ledger.provider_observation WHERE workspace_id=%s AND payout_id=%s "
        "AND kind='execution' AND observation_id<>%s ORDER BY source_sequence,observation_id",
        (row["workspace_id"], row["payout_id"], row["observation_id"]),
    ).fetchall()
    for other in others:
        if (
            row["state"] in TERMINAL
            and other["state"] in TERMINAL
            and row["state"] != other["state"]
        ):
            return "TERMINAL_CONTRADICTION", other
        if (
            row["state"] == "ACCEPTED"
            and other["state"] in TERMINAL
            and row["source_sequence"] > other["source_sequence"]
        ):
            return "NONTERMINAL_AFTER_FINAL", other
        if (
            row["state"] in TERMINAL
            and other["state"] == "ACCEPTED"
            and other["source_sequence"] > row["source_sequence"]
        ):
            return "NONTERMINAL_AFTER_FINAL", other
    return None, None


def _observe_nonterminal(cur, payout, row, intent, now, attempt_id):
    if row["state"] == "ACCEPTED" and row["source_sequence"] > payout["source_sequence"]:
        advance_knowledge(cur, payout, "ACCEPTED", row["source_sequence"])
    if attempt_id is not None and attempt_id != intent["last_attempt_id"]:
        return
    action = (
        "PUT" if row["state"] == "NOT_FOUND" and payout["knowledge"] != "ACCEPTED" else "LOOKUP"
    )
    cur.execute(
        "UPDATE ledger.dispatch_intent SET state='OBSERVING',next_action=%s,next_due_at=%s "
        "WHERE workspace_id=%s AND payout_id=%s",
        (action, now + timedelta(seconds=30), payout["workspace_id"], payout["operation_id"]),
    )


def _receive_once(
    conn, workspace, payout_id, context, observation, channel, attempt_id, clock, local_code=None
):
    with _transaction(conn, workspace) as cur:
        # Serialize provider-wide event/sequence comparisons without taking any
        # second workspace barrier or second payout lock. This costs per-provider
        # observation throughput; it is an explicit single-authority first version.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"ledgerops:provider:{context['provider_namespace']}",),
        )
        payout, intent = _lock_work(cur, workspace, payout_id)
        if payout is None or payout["provider_request"] != context["provider_request"]:
            raise InvariantViolation("Retained payout changed during observation validation")
        now = utc_instant(clock())
        row, new = _retain(cur, payout, observation, now)
        if row is None:
            return ObservationResult("BLOCKED", None, payout["knowledge"], "EVIDENCE_CAPACITY")
        _receipt(cur, row, channel, attempt_id, now)
        code, related = _contradiction(cur, row)
        if code is not None:
            _incident(cur, payout, "PROVIDER_CONTRADICTION", code, row, related)
            return ObservationResult("INCIDENT", row["observation_id"], payout["knowledge"], code)
        blocked = blocking_incidents(cur, workspace, payout_id)
        if blocked:
            _stop(cur, workspace, payout_id)
            return ObservationResult(
                "BLOCKED", row["observation_id"], payout["knowledge"], blocked[0]["code"]
            )
        resolution = cur.execute(
            "SELECT state FROM ledger.payout_resolution WHERE workspace_id=%s AND payout_id=%s",
            (workspace, payout_id),
        ).fetchone()
        if resolution is not None:
            # Compatible terminal duplicates and old nonterminal evidence retain
            # receipts; the original resolution and knowledge remain immutable.
            return ObservationResult("REPLAYED", row["observation_id"], resolution["state"])
        if local_code is not None:
            _incident(cur, payout, "LOCAL_BLOCKER", local_code, row)
            return ObservationResult(
                "BLOCKED", row["observation_id"], payout["knowledge"], local_code
            )
        if not new:
            return ObservationResult("REPLAYED", row["observation_id"], payout["knowledge"])
        if row["state"] in TERMINAL:
            settle(cur, payout, row, now)
            return ObservationResult("APPLIED", row["observation_id"], row["state"])
        _observe_nonterminal(cur, payout, row, intent, now, attempt_id)
        return ObservationResult("OBSERVED", row["observation_id"], row["state"])


def _quarantine(conn, workspace, payout_id, raw, channel, clock):
    with _transaction(conn, workspace) as cur:
        payout, _ = _lock_work(cur, workspace, payout_id)
        if payout is None:
            raise NotAuthorized()
        digest = hashlib.sha256(raw).digest()
        duplicate = cur.execute(
            "SELECT 1 FROM ledger.observation_quarantine WHERE workspace_id=%s AND payout_id=%s "
            "AND raw_sha256=%s AND raw_payload=%s LIMIT 1",
            (workspace, payout_id, digest, raw),
        ).fetchone()
        if duplicate is not None:
            return ObservationResult("QUARANTINED", None, None, "INVALID_OBSERVATION")
        count = cur.execute(
            "SELECT count(*) AS count FROM ledger.observation_quarantine "
            "WHERE workspace_id=%s AND payout_id=%s",
            (workspace, payout_id),
        ).fetchone()["count"]
        if count >= MAX_QUARANTINE:
            # Unauthenticated garbage cannot set a financial incident or stop a
            # valid payout just by filling this bounded diagnostic quarantine.
            return ObservationResult("QUARANTINED", None, None, "QUARANTINE_CAPACITY")
        cur.execute(
            "INSERT INTO ledger.observation_quarantine "
            "(workspace_id,quarantine_id,payout_id,channel,"
            "reason,raw_payload,raw_sha256,received_at) VALUES "
            "(%s,%s,%s,%s,'INVALID_OBSERVATION',%s,%s,%s)",
            (workspace, uuid4(), payout_id, channel, raw, digest, utc_instant(clock())),
        )
        return ObservationResult("QUARANTINED", None, None, "INVALID_OBSERVATION")


def receive_observation(
    conn,
    workspace,
    payout_id,
    raw,
    *,
    trust,
    channel="CALLBACK",
    transport_attempt_id=None,
    clock=utc_now,
):
    """Authenticate before relevance; atomically settle once or retain honest uncertainty.

    Expected local projection/capacity obstacles roll back the financial attempt,
    then retain evidence and a sticky blocker in a separate transaction. Arbitrary
    SQL failures still propagate with the complete financial transaction rolled back.
    """
    _idle(conn)
    workspace, payout_id = _identity(workspace, WorkspaceId), _identity(payout_id, OperationId)
    if type(raw) is not bytes or len(raw) > 65536:
        raise ValueError("observation adapter must supply at most65536 bytes")
    if type(trust) is not ProviderTrust or type(channel) is not str or channel not in CHANNELS:
        raise ValueError("trusted provider configuration and known receipt channel required")
    if transport_attempt_id is not None and type(transport_attempt_id) is not UUID:
        raise TypeError("transport attempt reference must be UUID")
    with conn.cursor(row_factory=dict_row) as cur:
        context = cur.execute(
            "SELECT provider_namespace,provider_key,provider_request FROM ledger.payout "
            "WHERE workspace_id=%s AND operation_id=%s",
            (workspace, payout_id),
        ).fetchone()
    if context is None:
        raise NotAuthorized()
    try:
        observation = verify_observation(
            raw,
            trust=trust,
            provider_key=context["provider_key"],
            request_bytes=context["provider_request"],
        )
    except InvalidObservation:
        return _retry(conn, lambda: _quarantine(conn, workspace, payout_id, raw, channel, clock))
    try:
        return _retry(
            conn,
            lambda: _receive_once(
                conn,
                workspace,
                payout_id,
                context,
                observation,
                channel,
                transport_attempt_id,
                clock,
            ),
        )
    except LocalSettlementBlocked as exc:
        local_code = exc.code
        return _retry(
            conn,
            lambda: _receive_once(
                conn,
                workspace,
                payout_id,
                context,
                observation,
                channel,
                transport_attempt_id,
                clock,
                local_code,
            ),
        )
