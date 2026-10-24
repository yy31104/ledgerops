"""Purchase admission in one Python-owned transaction with explicit SQL.

Principal is supplied by a trusted authentication boundary, not constructed from
untrusted request fields. This module implements neither HTTP nor token checking.
"""

import json
from dataclasses import dataclass
from random import uniform
from time import sleep
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledgerops.domain import (
    MAX_TOTAL_MINOR,
    ActorId,
    ClientId,
    PurchaseCommand,
    RequestKey,
    WorkspaceId,
)
from ledgerops.domain.requests import CANONICAL_PREFIX


@dataclass(frozen=True, slots=True)
class Principal:
    workspace_id: WorkspaceId
    client_id: ClientId
    actor_id: ActorId

    def __post_init__(self):
        for field, cls in (
            ("workspace_id", WorkspaceId),
            ("client_id", ClientId),
            ("actor_id", ActorId),
        ):
            if type(getattr(self, field)) is not cls:
                raise TypeError(f"{field} must be {cls.__name__}")


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    http_status: int
    body: dict
    replayed: bool = False


class AdmissionError(Exception):
    http_status = 500
    code = "INTERNAL_ERROR"
    retryable = False


class NotAuthorized(AdmissionError):
    http_status = 404
    code = "NOT_FOUND"

    def __init__(self):
        super().__init__("Purchase resource not found")


class IdempotencyConflict(AdmissionError):
    http_status = 409
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self):
        super().__init__("Request key belongs to a different purchase")


class BusinessIdConflict(AdmissionError):
    http_status = 409
    code = "BUSINESS_ID_CONFLICT"

    def __init__(self):
        super().__init__("Operation ID belongs to a different purchase")


class RetrySameIdentity(AdmissionError):
    http_status = 503
    code = "RETRY_SAME_IDENTITY"
    retryable = True

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Retry with the same operation ID and request key")


class InvariantViolation(RuntimeError):
    """Persisted/internal state violates the service contract; never a rejection."""


def _authorize_customer(cur, principal, command):
    row = cur.execute(
        """SELECT owner_id, class, normal_side, currency FROM ledger.account
           WHERE workspace_id = %s AND id = %s""",
        (command.workspace_id.value, command.customer_account_id.value),
    ).fetchone()
    if (
        row is None
        or row["owner_id"] != principal.actor_id.value
        or row["class"] != "CUSTOMER_LIABILITY"
        or row["normal_side"] != "C"
        or row["currency"] != "PLN"
    ):
        raise NotAuthorized()


def _authorize_stored(cur, principal, operation):
    if operation is None:
        raise InvariantViolation("Retained request has no operation")
    if operation["kind"] != "PURCHASE":
        raise NotAuthorized()
    try:
        payload = operation["payload"]
        if not payload.startswith(CANONICAL_PREFIX):
            raise ValueError("unknown canonical version")
        fields = json.loads(payload[len(CANONICAL_PREFIX) :])
        workspace = WorkspaceId.parse(fields.pop("workspace_id"))
        stored = PurchaseCommand.from_payload(workspace, fields)
        if (
            workspace != principal.workspace_id
            or stored.operation_id.value != operation["operation_id"]
            or stored.canonical_bytes != payload
        ):
            raise ValueError("invalid canonical identity")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise InvariantViolation("Invalid retained canonical purchase") from exc
    _authorize_customer(cur, principal, stored)
    return stored


def _retained_result(cur, operation, stored):
    status, body = operation["http_status"], operation["response_body"]
    journal = cur.execute(
        """SELECT journal_id, kind, line_count, total_minor FROM ledger.journal
           WHERE workspace_id = %s AND operation_id = %s""",
        (stored.workspace_id.value, stored.operation_id.value),
    ).fetchone()
    if status == 201 and operation["admission"] == "ACCEPTED" and journal is not None:
        expected = {
            "operation_id": str(stored.operation_id),
            "admission": "accepted",
            "journal_id": str(journal["journal_id"]),
            "amount_minor": str(stored.amount_minor.value),
            "currency": "PLN",
            "effect": "purchase_posted",
            "contract_version": 1,
        }
        if (
            body == expected
            and type(body.get("contract_version")) is int
            and journal["kind"] == "PURCHASE"
            and journal["line_count"] == 2
            and journal["total_minor"] == stored.amount_minor.value
        ):
            return AdmissionResult(status, body, replayed=True)
    if status == 422 and operation["admission"] == "REJECTED" and journal is None:
        error = body.get("error", {})
        try:
            request_id = str(UUID(error["request_id"]))
            expected = _rejection(stored, error["code"], request_id).body
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        else:
            if body == expected and error.get("retryable") is False:
                return AdmissionResult(status, body, replayed=True)
    raise InvariantViolation("Invalid retained purchase outcome")


def _rejection(command, code, request_id):
    messages = {
        "INSUFFICIENT_AVAILABLE": "Insufficient available balance",
        "LIMIT_EXCEEDED": "Purchase would exceed ledger limits",
    }
    return AdmissionResult(
        422,
        {
            "error": {
                "code": code,
                "message": messages[code],
                "retryable": False,
                "operation_id": str(command.operation_id),
                "request_id": request_id,
            }
        },
    )


def _finish_request(cur, scope, result):
    row = cur.execute(
        """UPDATE ledger.request_result SET http_status = %s, response_body = %s
           WHERE workspace_id = %s AND client_id = %s AND route_family = %s AND key = %s
           RETURNING 1""",
        (result.http_status, Jsonb(result.body), *scope),
    ).fetchone()
    if row is None:
        raise InvariantViolation("Request claim disappeared")


def _submit_purchase_once(
    conn: psycopg.Connection,
    principal: Principal,
    command: PurchaseCommand,
    request_key: RequestKey,
) -> AdmissionResult:
    """One complete attempt; return only after COMMIT and deferred checks.

    Authorization/identity errors roll back claims. Insufficient funds and bounded
    total/version exhaustion are committed business outcomes. The public entry point
    may retry a definitely aborted transaction; the caller still owns the connection.
    """
    if (
        type(principal) is not Principal
        or type(command) is not PurchaseCommand
        or type(request_key) is not RequestKey
    ):
        raise TypeError("purchase requires typed principal, command and request key")
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("purchase admission requires an idle autocommit connection")
    if (
        command.workspace_id != principal.workspace_id
        or request_key.workspace_id != principal.workspace_id
        or request_key.client_id != principal.client_id
    ):
        raise NotAuthorized()
    workspace = command.workspace_id.value
    operation_id = command.operation_id.value
    payload, digest = command.canonical_bytes, command.payload_sha256
    scope = (workspace, principal.client_id.value, request_key.route_family, request_key.key)

    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
            cur.execute("SELECT set_config('lock_timeout', '5s', true)")
            cur.execute("SELECT set_config('statement_timeout', '10s', true)")
            cur.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
                (f"ledgerops:workspace:{command.workspace_id}",),
            )
            ws = cur.execute(
                "SELECT mode FROM ledger.workspace WHERE id = %s", (workspace,)
            ).fetchone()
            if ws is None:
                raise NotAuthorized()
            if ws["mode"] != "NORMAL":
                raise RetrySameIdentity("workspace_not_normal")
            _authorize_customer(cur, principal, command)

            claimed_request = cur.execute(
                """INSERT INTO ledger.request_result
                   (workspace_id, client_id, route_family, key,
                    operation_id, payload, payload_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT ON CONSTRAINT request_result_pkey DO NOTHING RETURNING 1""",
                (*scope, operation_id, payload, digest),
            ).fetchone()
            if claimed_request is None:
                retained = cur.execute(
                    """SELECT operation_id, payload, http_status, response_body
                       FROM ledger.request_result
                       WHERE workspace_id=%s AND client_id=%s AND route_family=%s AND key=%s""",
                    scope,
                ).fetchone()
                if retained is None:
                    raise InvariantViolation("Committed request disappeared")
                op = cur.execute(
                    "SELECT * FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s",
                    (workspace, retained["operation_id"]),
                ).fetchone()
                stored = _authorize_stored(cur, principal, op)
                if retained["payload"] != payload:
                    raise IdempotencyConflict()
                if any(
                    retained[field] != op[field] for field in ("payload", "http_status")
                ) or json.dumps(retained["response_body"], sort_keys=True) != json.dumps(
                    op["response_body"], sort_keys=True
                ):
                    raise InvariantViolation("Request and operation disagree")
                result = _retained_result(cur, op, stored)
            else:
                claimed_operation = cur.execute(
                    """INSERT INTO ledger.operation
                       (workspace_id, operation_id, kind, payload, payload_sha256)
                       VALUES (%s, %s, 'PURCHASE', %s, %s)
                       ON CONFLICT ON CONSTRAINT operation_pkey DO NOTHING RETURNING 1""",
                    (workspace, operation_id, payload, digest),
                ).fetchone()
                if claimed_operation is None:
                    op = cur.execute(
                        """SELECT * FROM ledger.operation
                           WHERE workspace_id=%s AND operation_id=%s FOR UPDATE""",
                        (workspace, operation_id),
                    ).fetchone()
                    stored = _authorize_stored(cur, principal, op)
                    if op["payload"] != payload:
                        raise BusinessIdConflict()
                    result = _retained_result(cur, op, stored)
                    _finish_request(cur, scope, result)
                else:
                    balances = {}
                    for account in sorted(
                        (command.customer_account_id, command.merchant_account_id),
                        key=lambda account: account.value.int,
                    ):
                        row = cur.execute(
                            """SELECT a.owner_id, a.class, a.normal_side, a.currency, a.enabled,
                                      b.debits, b.credits, b.booked, b.reserved, b.version
                               FROM ledger.account a JOIN ledger.account_balance b
                                 ON b.workspace_id=a.workspace_id AND b.account_id=a.id
                               WHERE a.workspace_id=%s AND a.id=%s FOR UPDATE OF b""",
                            (workspace, account.value),
                        ).fetchone()
                        if row is None:
                            exists = cur.execute(
                                "SELECT 1 FROM ledger.account WHERE workspace_id=%s AND id=%s",
                                (workspace, account.value),
                            ).fetchone()
                            if exists is not None:
                                raise InvariantViolation("Account projection missing")
                            raise NotAuthorized()
                        if (
                            not row["enabled"]
                            or row["currency"] != "PLN"
                            or row["normal_side"] != "C"
                        ):
                            raise NotAuthorized()
                        balances[account] = row
                    customer = balances[command.customer_account_id]
                    merchant = balances[command.merchant_account_id]
                    if (
                        customer["owner_id"] != principal.actor_id.value
                        or customer["class"] != "CUSTOMER_LIABILITY"
                        or merchant["class"] != "MERCHANT_LIABILITY"
                    ):
                        raise NotAuthorized()
                    amount = command.amount_minor.value
                    # Convert exact domain integers before arithmetic: Decimal's
                    # ambient precision must not round 38-digit cumulative totals.
                    debit_after = int(customer["debits"]) + amount
                    credit_after = int(merchant["credits"]) + amount
                    if int(customer["booked"]) - int(customer["reserved"]) < amount:
                        result = _rejection(command, "INSUFFICIENT_AVAILABLE", str(uuid4()))
                    elif (
                        debit_after > MAX_TOTAL_MINOR
                        or credit_after > MAX_TOTAL_MINOR
                        or any(row["version"] >= 2**63 - 1 for row in balances.values())
                    ):
                        result = _rejection(command, "LIMIT_EXCEEDED", str(uuid4()))
                    else:
                        journal_id = uuid4()
                        cur.execute(
                            """INSERT INTO ledger.journal
                               (workspace_id,journal_id,operation_id,kind,line_count,total_minor)
                               VALUES (%s,%s,%s,'PURCHASE',2,%s)""",
                            (workspace, journal_id, operation_id, amount),
                        )
                        cur.execute(
                            """INSERT INTO ledger.journal_entry
                               (workspace_id,journal_id,line_no,account_id,side,amount_minor)
                               VALUES (%s,%s,1,%s,'D',%s), (%s,%s,2,%s,'C',%s)""",
                            (
                                workspace,
                                journal_id,
                                command.customer_account_id.value,
                                amount,
                                workspace,
                                journal_id,
                                command.merchant_account_id.value,
                                amount,
                            ),
                        )
                        for account in sorted(balances, key=lambda account: account.value.int):
                            delta = (
                                (amount, 0)
                                if account == command.customer_account_id
                                else (0, amount)
                            )
                            updated = cur.execute(
                                """UPDATE ledger.account_balance
                                   SET debits=debits+%s, credits=credits+%s, version=version+1
                                   WHERE workspace_id=%s AND account_id=%s RETURNING 1""",
                                (*delta, workspace, account.value),
                            ).fetchone()
                            if updated is None:
                                raise InvariantViolation("Locked account projection disappeared")
                        result = AdmissionResult(
                            201,
                            {
                                "operation_id": str(command.operation_id),
                                "admission": "accepted",
                                "journal_id": str(journal_id),
                                "amount_minor": str(amount),
                                "currency": "PLN",
                                "effect": "purchase_posted",
                                "contract_version": 1,
                            },
                        )
                    updated = cur.execute(
                        """UPDATE ledger.operation
                           SET admission=%s, http_status=%s, response_body=%s
                           WHERE workspace_id=%s AND operation_id=%s RETURNING 1""",
                        (
                            "ACCEPTED" if result.http_status == 201 else "REJECTED",
                            result.http_status,
                            Jsonb(result.body),
                            workspace,
                            operation_id,
                        ),
                    ).fetchone()
                    if updated is None:
                        raise InvariantViolation("Operation claim disappeared")
                    _finish_request(cur, scope, result)
        return result
    except (
        psycopg.errors.LockNotAvailable,
        psycopg.errors.QueryCanceled,
    ) as exc:
        raise RetrySameIdentity("database_contention") from exc


def submit_purchase(
    conn: psycopg.Connection,
    principal: Principal,
    command: PurchaseCommand,
    request_key: RequestKey,
) -> AdmissionResult:
    """Try at most three whole transactions after definite deadlock/serialization aborts.

    Retry only SQLSTATE 40P01/40001, with unchanged typed identities and no open
    transaction during backoff. Timeouts/cancellation and integrity errors are not
    internally retried. Connection loss, including during COMMIT, propagates: the
    caller must reconnect and resubmit the same identities to resolve the outcome.
    """
    for attempt in range(1, 4):
        try:
            return _submit_purchase_once(conn, principal, command, request_key)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure) as exc:
            # Never proceed if rollback did not restore a usable idle connection.
            if conn.closed or conn.info.transaction_status != TransactionStatus.IDLE:
                raise
            if attempt == 3:
                raise RetrySameIdentity("transaction_retries_exhausted") from exc
            sleep(uniform(0.01, 0.05) * attempt)
    raise AssertionError("unreachable retry state")
