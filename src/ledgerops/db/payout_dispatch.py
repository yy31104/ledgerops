"""Durable claim/transport boundaries; PR02 never consumes or releases a hold."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from random import uniform
from time import sleep
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from ledgerops.db.admission import InvariantViolation, RetrySameIdentity, _parse_stored
from ledgerops.db.payout_admission import (
    retained_payout_result,
    settlement_capacity,
    utc_instant,
    utc_now,
)
from ledgerops.domain import OperationId, ProviderNamespaceId, WorkspaceId
from ledgerops.provider_client import TransportResponse
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust, verify_observation


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    workspace_id: UUID
    payout_id: UUID
    attempt_id: UUID
    provider_key: str
    request_bytes: bytes
    action: str
    dispatch_deadline: object


@dataclass(frozen=True, slots=True)
class DispatchResult:
    claim: DispatchClaim
    outcome: str
    http_status: int | None
    observation: object | None
    resolution: object | None = None


def _identity(value, cls):
    if type(value) is cls:
        return value.value
    if type(value) is UUID:
        return value
    raise TypeError(f"requires {cls.__name__} or UUID")


def _idle(conn):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("payout worker requires an idle autocommit connection")


@contextmanager
def _transaction(conn, workspace):
    _idle(conn)
    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
            cur.execute("SELECT set_config('lock_timeout','5s',true)")
            cur.execute("SELECT set_config('statement_timeout','10s',true)")
            cur.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                (f"ledgerops:workspace:{workspace}",),
            )
            row = cur.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone()
            if row is None or row["mode"] != "NORMAL":
                raise RetrySameIdentity("workspace_not_normal")
            yield cur
    except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as exc:
        raise RetrySameIdentity("database_contention") from exc


def _retry(conn, call):
    for attempt in range(1, 4):
        try:
            return call()
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure) as exc:
            if conn.closed or conn.info.transaction_status != TransactionStatus.IDLE:
                raise
            if attempt == 3:
                raise RetrySameIdentity("transaction_retries_exhausted") from exc
            sleep(uniform(0.01, 0.05) * attempt)
    raise AssertionError("unreachable retry state")


def _lock_work(cur, workspace, payout_id):
    op = cur.execute(
        "SELECT * FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s FOR UPDATE",
        (workspace, payout_id),
    ).fetchone()
    if op is None:
        return None, None
    payout = cur.execute(
        "SELECT * FROM ledger.payout WHERE workspace_id=%s AND operation_id=%s FOR UPDATE",
        (workspace, payout_id),
    ).fetchone()
    if payout is None:
        return None, None
    intent = cur.execute(
        "SELECT * FROM ledger.dispatch_intent WHERE workspace_id=%s AND payout_id=%s FOR UPDATE",
        (workspace, payout_id),
    ).fetchone()
    if intent is None or (op["kind"], op["admission"], op["http_status"]) != (
        "PAYOUT",
        "ACCEPTED",
        202,
    ):
        raise InvariantViolation("Incomplete admitted payout")
    # The runtime role can make structurally valid mistakes. Do not turn a
    # provider instruction that disagrees with retained business intent into an
    # external payment merely because its digest and hold constraints are valid.
    stored = _parse_stored(op, WorkspaceId(workspace))
    retained_payout_result(cur, op, stored)
    return payout, intent


def _preflight(cur, payout):
    merchant = cur.execute(
        "SELECT debits,credits,booked,reserved,version FROM ledger.account_balance WHERE "
        "workspace_id=%s AND account_id=%s",
        (payout["workspace_id"], payout["merchant_account_id"]),
    ).fetchone()
    if (
        merchant is None
        or int(merchant["reserved"]) < payout["gross_minor"]
        or int(merchant["booked"]) < int(merchant["reserved"])
        or not settlement_capacity(cur, payout["workspace_id"], merchant, payout["gross_minor"])
    ):
        raise RetrySameIdentity("settlement_capacity_unavailable")


def claim_payout(conn, workspace, payout_id, *, clock=utc_now, lease_seconds=30):
    workspace, payout_id = _identity(workspace, WorkspaceId), _identity(payout_id, OperationId)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise ValueError("claim lease must be 1 to300 seconds")

    def once():
        with _transaction(conn, workspace) as cur:
            payout, intent = _lock_work(cur, workspace, payout_id)
            if payout is None:
                return None
            from ledgerops.db.payout_resolution import blocking_incidents

            if intent["state"] in {"DONE", "BLOCKED"} or blocking_incidents(
                cur, workspace, payout_id
            ):
                return None
            now = utc_instant(clock())
            if now < intent["next_due_at"]:
                return None
            if payout["version"] >= 2**63 - 1 or intent["attempts"] >= 2**63 - 1:
                raise RetrySameIdentity("dispatch_capacity_unavailable")
            action = "CLOSE" if now >= payout["dispatch_deadline"] else intent["next_action"]
            if payout["knowledge"] == "ACCEPTED" and action != "CLOSE":
                action = "LOOKUP"
            # A committed claim whose process disappeared must observe before resending.
            if intent["state"] == "IN_FLIGHT" and action != "CLOSE":
                action = "LOOKUP"
            if action == "PUT":
                _preflight(cur, payout)
            attempt_id = uuid4()
            cur.execute(
                "INSERT INTO ledger.transport_attempt "
                "(workspace_id,attempt_id,payout_id,action,started_at) VALUES (%s,%s,%s,%s,%s)",
                (workspace, attempt_id, payout_id, action, now),
            )
            cur.execute(
                "UPDATE ledger.payout SET knowledge=CASE WHEN knowledge='NOT_SENT' "
                "THEN 'UNKNOWN' ELSE knowledge END,version=version+1 WHERE "
                "workspace_id=%s AND operation_id=%s",
                (workspace, payout_id),
            )
            cur.execute(
                "UPDATE ledger.dispatch_intent SET "
                "state='IN_FLIGHT',next_action='LOOKUP',next_due_at=%s,"
                "attempts=attempts+1,last_attempt_id=%s WHERE workspace_id=%s AND payout_id=%s",
                (now + timedelta(seconds=lease_seconds), attempt_id, workspace, payout_id),
            )
            claim = DispatchClaim(
                workspace,
                payout_id,
                attempt_id,
                payout["provider_key"],
                payout["provider_request"],
                action,
                payout["dispatch_deadline"],
            )
        return claim

    return _retry(conn, once)


def due_payouts(conn, *, clock=utc_now, limit=100, provider_namespace=None, after_workspace=None):
    """Interleave workspaces in bounded discovery; claims still recheck under locks.

    The caller advances its workspace cursor even when capacity blocks a claim.
    One large blocked workspace cannot consume every slot, and a circular cursor
    prevents a fixed UUID prefix from monopolizing successive small batches.
    """
    _idle(conn)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("candidate batch must contain 1 to1000 items")
    namespace = (
        None if provider_namespace is None else _identity(provider_namespace, ProviderNamespaceId)
    )
    after = None if after_workspace is None else _identity(after_workspace, WorkspaceId)
    return conn.execute(
        "WITH due AS (SELECT i.workspace_id,i.payout_id,i.next_due_at,"
        "row_number() OVER (PARTITION BY i.workspace_id ORDER BY i.next_due_at,i.payout_id) "
        "AS workspace_rank FROM ledger.dispatch_intent i JOIN ledger.workspace w "
        "ON w.id=i.workspace_id JOIN ledger.payout p "
        "ON p.workspace_id=i.workspace_id AND p.operation_id=i.payout_id "
        "WHERE w.mode='NORMAL' AND i.next_due_at<=%s "
        "AND (%s::uuid IS NULL OR p.provider_namespace=%s::uuid) "
        "AND i.state IN ('READY','IN_FLIGHT','OBSERVING') "
        "AND NOT EXISTS (SELECT 1 FROM ledger.unresolved_payout_incident incident "
        "WHERE incident.workspace_id=i.workspace_id AND incident.payout_id=i.payout_id) "
        "AND NOT EXISTS (SELECT 1 FROM ledger.unresolved_payout_incident incident "
        "JOIN ledger.provider_observation o ON o.observation_id=incident.related_observation_id "
        "WHERE o.workspace_id=i.workspace_id AND o.payout_id=i.payout_id)) "
        "SELECT workspace_id,payout_id FROM due ORDER BY workspace_rank,"
        "CASE WHEN %s::uuid IS NULL OR workspace_id>%s::uuid THEN 0 ELSE 1 END,"
        "workspace_id,next_due_at,payout_id LIMIT %s",
        (utc_instant(clock()), namespace, namespace, after, after, limit),
    ).fetchall()


def finish_claim(
    conn, claim, *, outcome, response=None, next_action="LOOKUP", clock=utc_now, jitter=uniform
):
    """Finish only this attempt; an old worker cannot overwrite a newer claim."""
    if type(claim) is not DispatchClaim or next_action not in {"PUT", "LOOKUP", "CLOSE"}:
        raise TypeError("typed claim and valid next action required")
    if outcome not in {
        "RESPONSE",
        "TIMEOUT",
        "CONNECTION_ERROR",
        "TRANSPORT_ERROR",
        "INVALID_RESPONSE",
    }:
        raise ValueError("invalid completed attempt outcome")
    if response is not None and type(response) is not TransportResponse:
        raise TypeError("response must be bounded TransportResponse")
    if outcome in {"RESPONSE", "INVALID_RESPONSE"} and response is None:
        raise ValueError("response outcome requires retained bytes")
    status, raw = (None, None) if response is None else (response.http_status, response.body)

    def once():
        with _transaction(conn, claim.workspace_id) as cur:
            payout, intent = _lock_work(cur, claim.workspace_id, claim.payout_id)
            attempt = cur.execute(
                "SELECT * FROM ledger.transport_attempt WHERE workspace_id=%s AND attempt_id=%s "
                "FOR UPDATE",
                (claim.workspace_id, claim.attempt_id),
            ).fetchone()
            if (
                payout is None
                or attempt is None
                or attempt["payout_id"] != claim.payout_id
                or attempt["action"] != claim.action
                or payout["provider_key"] != claim.provider_key
                or payout["provider_request"] != claim.request_bytes
            ):
                raise InvariantViolation("Attempt does not match retained payout instruction")
            if attempt["outcome"] != "MAY_HAVE_SENT":
                if (attempt["outcome"], attempt["http_status"], attempt["response_payload"]) != (
                    outcome,
                    status,
                    raw,
                ):
                    raise InvariantViolation("Attempt completion cannot be replaced")
                return False
            now = utc_instant(clock())
            cur.execute(
                "UPDATE ledger.transport_attempt SET "
                "finished_at=%s,outcome=%s,http_status=%s,response_payload=%s WHERE "
                "workspace_id=%s AND attempt_id=%s",
                (now, outcome, status, raw, claim.workspace_id, claim.attempt_id),
            )
            if intent["last_attempt_id"] == claim.attempt_id and intent["state"] == "IN_FLIGHT":
                # At most five calls in the initial active round, then one bounded
                # observation/retry call per minute. Exhaustion never releases funds.
                base_delay = (
                    min(30, 2 ** min(intent["attempts"], 5)) if intent["attempts"] < 5 else 60
                )
                factor = jitter(0.8, 1.2)
                if type(factor) not in (int, float) or not 0.8 <= factor <= 1.2:
                    raise ValueError("retry jitter must stay within its configured bounds")
                delay = min(60, base_delay * factor)
                if now >= payout["dispatch_deadline"]:
                    next_step = "CLOSE"
                elif payout["knowledge"] == "ACCEPTED":
                    next_step = "LOOKUP"
                else:
                    next_step = next_action
                cur.execute(
                    "UPDATE ledger.dispatch_intent SET "
                    "state='OBSERVING',next_action=%s,next_due_at=%s WHERE workspace_id=%s AND "
                    "payout_id=%s",
                    (
                        next_step,
                        now + timedelta(seconds=delay),
                        claim.workspace_id,
                        claim.payout_id,
                    ),
                )
            return True

    return _retry(conn, once)


def _transport(client, claim):
    if claim.action == "PUT":
        return client.put(claim.provider_key, claim.request_bytes)
    if claim.action == "LOOKUP":
        return client.lookup(claim.provider_key)
    return client.close(claim.provider_key, claim.request_bytes)


def dispatch_once(
    conn, workspace, payout_id, client, *, trust, clock=utc_now, lease_seconds=30, jitter=uniform
):
    """One bounded call after a committed claim, followed by a separate DB write."""
    if type(trust) is not ProviderTrust:
        raise TypeError("dispatch requires trusted provider configuration")
    claim = claim_payout(conn, workspace, payout_id, clock=clock, lease_seconds=lease_seconds)
    if claim is None:
        return None
    # Recheck at the actual send boundary. A pause after claim must not create a
    # new PUT beyond its cutoff; it must take a new durable CLOSE claim instead.
    if claim.action == "PUT" and utc_instant(clock()) >= claim.dispatch_deadline:
        finish_claim(
            conn, claim, outcome="TRANSPORT_ERROR", next_action="CLOSE", clock=clock, jitter=jitter
        )
        return DispatchResult(claim, "TRANSPORT_ERROR", None, None)
    if not claim.provider_key.startswith(f"v1.{trust.namespace}."):
        finish_claim(conn, claim, outcome="TRANSPORT_ERROR", clock=clock, jitter=jitter)
        raise ValueError("worker provider configuration does not match retained namespace")
    response, observation, next_action = None, None, "LOOKUP"
    try:
        _idle(conn)
        response = _transport(client, claim)
        if type(response) is not TransportResponse:
            raise TypeError("provider client must return bounded TransportResponse")
        if response.http_status == 200:
            try:
                observation = verify_observation(
                    response.body,
                    trust=trust,
                    provider_key=claim.provider_key,
                    request_bytes=claim.request_bytes,
                )
                outcome = "RESPONSE"
                if observation.state == "NOT_FOUND":
                    next_action = "PUT"
            except InvalidObservation:
                outcome = "INVALID_RESPONSE"
        else:
            outcome = "RESPONSE"
            if claim.action == "LOOKUP" and response.http_status >= 500:
                next_action = "PUT"
    except TimeoutError:
        outcome = "TIMEOUT"
        if claim.action == "LOOKUP":
            next_action = "PUT"
    except ConnectionError:
        outcome = "CONNECTION_ERROR"
        if claim.action == "LOOKUP":
            next_action = "PUT"
    finish_claim(
        conn,
        claim,
        outcome=outcome,
        response=response,
        next_action=next_action,
        clock=clock,
        jitter=jitter,
    )
    resolution = None
    if response is not None:
        from ledgerops.db.payout_resolution import receive_observation

        resolution = receive_observation(
            conn,
            claim.workspace_id,
            claim.payout_id,
            response.body,
            trust=trust,
            channel=claim.action,
            transport_attempt_id=claim.attempt_id,
            clock=clock,
        )
    return DispatchResult(
        claim, outcome, None if response is None else response.http_status, observation, resolution
    )
