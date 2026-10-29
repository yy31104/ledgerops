"""Payout-specific business steps inside the shared admission transaction."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from ledgerops.db.admission import (
    AdmissionResult,
    InvariantViolation,
    NotAuthorized,
    Principal,
    _finish_operation,
    _rejection,
    _submit_once,
)
from ledgerops.domain import (
    MAX_TOTAL_MINOR,
    OperationId,
    PayoutCommand,
    TrustedPayoutPolicy,
)


def utc_now():
    return datetime.now(UTC)


def utc_instant(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware UTC-convertible datetime")
    return value.astimezone(UTC)


def deadline_text(value):
    value = utc_instant(value)
    if value.microsecond:
        raise InvariantViolation("stored dispatch deadline must use whole UTC seconds")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def provider_instruction(command, deadline):
    request = {
        "workspace_id": str(command.workspace_id),
        "operation_id": str(command.operation_id),
        "merchant_id": str(command.merchant_account_id),
        "beneficiary_id": str(command.beneficiary_id),
        "principal_minor": str(command.principal_minor.value),
        "fee_minor": str(command.fee_minor),
        "gross_minor": str(command.gross_minor),
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": deadline_text(deadline),
    }
    key = f"v1.{command.provider_namespace}.{command.workspace_id}.{command.operation_id}"
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return key, raw


def accepted_body(command, deadline):
    return {
        "operation_id": str(command.operation_id),
        "payout_id": str(command.operation_id),
        "admission": "accepted_for_processing",
        "principal_minor": str(command.principal_minor.value),
        "fee_minor": str(command.fee_minor),
        "gross_minor": str(command.gross_minor),
        "currency": "PLN",
        "dispatch_deadline": deadline_text(deadline),
        "contract_version": 1,
    }


def _lock_merchant(cur, principal, command):
    row = cur.execute(
        "SELECT a.owner_id,a.class,a.normal_side,a.currency,a.enabled,b.debits,b.credits,"
        "b.booked,b.reserved,b.version FROM ledger.account a JOIN ledger.account_balance b "
        "ON b.workspace_id=a.workspace_id AND b.account_id=a.id "
        "WHERE a.workspace_id=%s AND a.id=%s FOR UPDATE OF b",
        (command.workspace_id.value, command.merchant_account_id.value),
    ).fetchone()
    if row is None:
        raise InvariantViolation("Authorized merchant projection is missing")
    if not row["enabled"] or (
        row["owner_id"],
        row["class"],
        row["normal_side"],
        row["currency"],
    ) != (principal.actor_id.value, "MERCHANT_LIABILITY", "C", "PLN"):
        raise NotAuthorized()
    return row


def settlement_capacity(cur, workspace, merchant, gross):
    """Conservative preflight, not a promise against future concurrent corruption."""
    clearing = cur.execute(
        "SELECT b.debits,b.credits,b.booked,b.reserved,b.version FROM ledger.account a "
        "LEFT JOIN ledger.account_balance b ON b.workspace_id=a.workspace_id AND b.account_id=a.id "
        "WHERE a.workspace_id=%s AND a.class='SYNTHETIC_CLEARING' AND a.normal_side='D' "
        "AND a.currency='PLN'",
        (workspace,),
    ).fetchall()
    if len(clearing) != 1 or clearing[0]["booked"] is None:
        raise InvariantViolation("Exactly one clearing projection is required")
    row = clearing[0]
    return (
        int(merchant["debits"]) + gross <= MAX_TOTAL_MINOR
        and int(row["credits"]) + gross <= MAX_TOTAL_MINOR
        and int(row["booked"]) - int(row["reserved"]) >= gross
        and merchant["version"] <= 2**63 - 3
        and row["version"] < 2**63 - 1
    )


def _insert_hold(cur, command, now, deadline):
    workspace, payout = command.workspace_id.value, command.operation_id.value
    key, raw = provider_instruction(command, deadline)
    cur.execute(
        "INSERT INTO ledger.payout (workspace_id,operation_id,merchant_account_id,beneficiary_id,"
        "provider_namespace,provider_key,principal_minor,fee_minor,gross_minor,currency,contract_version,"
        "dispatch_deadline,provider_request,provider_request_sha256) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLN',1,%s,%s,%s)",
        (
            workspace,
            payout,
            command.merchant_account_id.value,
            command.beneficiary_id.value,
            command.provider_namespace.value,
            key,
            command.principal_minor.value,
            command.fee_minor,
            command.gross_minor,
            deadline,
            raw,
            hashlib.sha256(raw).digest(),
        ),
    )
    cur.execute(
        "INSERT INTO ledger.reservation (workspace_id,payout_id,account_id,gross_minor) VALUES "
        "(%s,%s,%s,%s)",
        (workspace, payout, command.merchant_account_id.value, command.gross_minor),
    )
    cur.execute(
        "INSERT INTO ledger.reservation_event (workspace_id,payout_id,event_no,state) VALUES "
        "(%s,%s,1,'HELD')",
        (workspace, payout),
    )
    cur.execute(
        "UPDATE ledger.account_balance SET reserved=reserved+%s,version=version+1 "
        "WHERE workspace_id=%s AND account_id=%s",
        (command.gross_minor, workspace, command.merchant_account_id.value),
    )
    cur.execute(
        "INSERT INTO ledger.dispatch_intent (workspace_id,payout_id,next_due_at) VALUES (%s,%s,%s)",
        (workspace, payout, now),
    )
    return AdmissionResult(202, accepted_body(command, deadline))


def payout_submitter(policy, clock):
    if type(policy) is not TrustedPayoutPolicy:
        raise TypeError("payout requires trusted policy configuration")
    clock = utc_now if clock is None else clock
    if not callable(clock):
        raise TypeError("clock must be callable")
    # A definitely aborted database retry reuses the first sampled deadline. A
    # retained replay never samples the current clock or revalidates today's quote.
    timing = []

    def post(cur, principal, command, scope):
        if not policy.permits(command, principal.actor_id):
            raise NotAuthorized()
        row = _lock_merchant(cur, principal, command)
        if int(row["booked"]) - int(row["reserved"]) < command.gross_minor:
            result = _rejection(command, "INSUFFICIENT_AVAILABLE", str(uuid4()))
        elif not settlement_capacity(cur, command.workspace_id.value, row, command.gross_minor):
            result = _rejection(command, "LIMIT_EXCEEDED", str(uuid4()))
        else:
            if not timing:
                now = utc_instant(clock()).replace(microsecond=0)
                timing.append((now, now + timedelta(seconds=policy.dispatch_ttl_seconds)))
            result = _insert_hold(cur, command, *timing[0])
        return _finish_operation(cur, command, scope, result)

    def submit_once(conn, principal, command, request_key):
        return _submit_once(conn, principal, command, request_key, PayoutCommand, post_new=post)

    return submit_once


def retained_payout_result(cur, operation, command):
    params = (command.workspace_id.value, command.operation_id.value)
    payout = cur.execute(
        "SELECT * FROM ledger.payout WHERE workspace_id=%s AND operation_id=%s", params
    ).fetchone()
    status, body = operation["http_status"], operation["response_body"]
    if status == 422 and operation["admission"] == "REJECTED" and payout is None:
        try:
            error = body["error"]
            expected = _rejection(command, error["code"], str(UUID(error["request_id"]))).body
            if body == expected and error["retryable"] is False:
                return AdmissionResult(422, body, replayed=True)
        except (KeyError, ValueError, TypeError):
            pass
    if status == 202 and operation["admission"] == "ACCEPTED" and payout is not None:
        key, raw = provider_instruction(command, payout["dispatch_deadline"])
        hold = cur.execute(
            "SELECT * FROM ledger.reservation WHERE workspace_id=%s AND payout_id=%s", params
        ).fetchone()
        event = cur.execute(
            "SELECT 1 FROM ledger.reservation_event WHERE workspace_id=%s AND payout_id=%s AND "
            "event_no=1 AND state='HELD'",
            params,
        ).fetchone()
        intent = cur.execute(
            "SELECT 1 FROM ledger.dispatch_intent WHERE workspace_id=%s AND payout_id=%s", params
        ).fetchone()
        if (
            body == accepted_body(command, payout["dispatch_deadline"])
            and type(body.get("contract_version")) is int
            and payout["provider_key"] == key
            and payout["provider_request"] == raw
            and payout["provider_request_sha256"] == hashlib.sha256(raw).digest()
            and (
                payout["merchant_account_id"],
                payout["beneficiary_id"],
                payout["provider_namespace"],
                payout["principal_minor"],
                payout["fee_minor"],
                payout["gross_minor"],
            )
            == (
                command.merchant_account_id.value,
                command.beneficiary_id.value,
                command.provider_namespace.value,
                command.principal_minor.value,
                command.fee_minor,
                command.gross_minor,
            )
            and hold is not None
            and hold["account_id"] == command.merchant_account_id.value
            and hold["gross_minor"] == command.gross_minor
            and event is not None
            and intent is not None
        ):
            return AdmissionResult(202, body, replayed=True)
    raise InvariantViolation("Invalid retained payout outcome")


def payout_state(conn, principal, payout_id):
    if type(principal) is not Principal or type(payout_id) is not OperationId:
        raise TypeError("payout state requires typed principal and operation ID")
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("payout state requires idle autocommit connection")
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT p.operation_id,p.principal_minor,p.fee_minor,p.gross_minor,p.dispatch_deadline,"
            "p.knowledge,r.state AS reservation_state,i.state AS "
            "dispatch_state,i.attempts,i.next_due_at "
            "FROM ledger.payout p JOIN ledger.account a ON a.workspace_id=p.workspace_id AND "
            "a.id=p.merchant_account_id "
            "JOIN ledger.reservation r ON r.workspace_id=p.workspace_id AND "
            "r.payout_id=p.operation_id "
            "JOIN ledger.dispatch_intent i ON i.workspace_id=p.workspace_id AND "
            "i.payout_id=p.operation_id "
            "WHERE p.workspace_id=%s AND p.operation_id=%s AND a.owner_id=%s",
            (principal.workspace_id.value, payout_id.value, principal.actor_id.value),
        ).fetchone()
    if row is None:
        raise NotAuthorized()
    return {
        "payout_id": str(row["operation_id"]),
        "principal_minor": str(row["principal_minor"]),
        "fee_minor": str(row["fee_minor"]),
        "gross_minor": str(row["gross_minor"]),
        "dispatch_deadline": deadline_text(row["dispatch_deadline"]),
        "knowledge": row["knowledge"],
        "reservation_state": row["reservation_state"],
        "dispatch_state": row["dispatch_state"],
        "attempts": row["attempts"],
        "next_due_at": row["next_due_at"].isoformat(),
    }
