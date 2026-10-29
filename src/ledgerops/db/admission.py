"""Purchase and reversal admission in one Python-owned transaction with explicit SQL.

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
    PayoutCommand,
    PurchaseCommand,
    RequestKey,
    ReversalCommand,
    WorkspaceId,
)
from ledgerops.domain.payouts import PAYOUT_PREFIX, PAYOUT_ROUTE
from ledgerops.domain.requests import (
    CANONICAL_PREFIX,
    PURCHASE_ROUTE,
    REVERSAL_PREFIX,
    REVERSAL_ROUTE,
)


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


def _parse_stored(operation, workspace):
    kind = operation["kind"]
    codecs = {
        "PURCHASE": (PurchaseCommand, CANONICAL_PREFIX),
        "REVERSAL": (ReversalCommand, REVERSAL_PREFIX),
        "PAYOUT": (PayoutCommand, PAYOUT_PREFIX),
    }
    if kind not in codecs:
        raise NotAuthorized()
    command_type, prefix = codecs[kind]
    try:
        payload = operation["payload"]
        if not payload.startswith(prefix):
            raise ValueError("unknown canonical version")
        fields = json.loads(payload[len(prefix) :])
        stored_workspace = WorkspaceId.parse(fields.pop("workspace_id"))
        stored = command_type.from_payload(stored_workspace, fields)
        if (
            stored_workspace != workspace
            or stored.operation_id.value != operation["operation_id"]
            or stored.canonical_bytes != payload
        ):
            raise ValueError("invalid canonical identity")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise InvariantViolation(f"Invalid retained canonical {kind.lower()}") from exc
    return stored


def _original_purchase(cur, principal, command, *, lock=False):
    """Read immutable original; only a newly admitted reversal requests its row lock."""
    operation = cur.execute(
        "SELECT * FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (command.workspace_id.value, command.original_operation_id.value),
    ).fetchone()
    if operation is None or operation["kind"] != "PURCHASE" or operation["admission"] != "ACCEPTED":
        raise NotAuthorized()
    original = _parse_stored(operation, command.workspace_id)
    merchant = cur.execute(
        "SELECT owner_id,class,normal_side,currency FROM ledger.account "
        "WHERE workspace_id=%s AND id=%s",
        (command.workspace_id.value, original.merchant_account_id.value),
    ).fetchone()
    if (
        merchant is None
        or merchant["class"] != "MERCHANT_LIABILITY"
        or merchant["normal_side"] != "C"
        or merchant["currency"] != "PLN"
        or (principal is not None and merchant["owner_id"] != principal.actor_id.value)
    ):
        raise NotAuthorized()
    customer = cur.execute(
        "SELECT class,normal_side,currency FROM ledger.account WHERE workspace_id=%s AND id=%s",
        (command.workspace_id.value, original.customer_account_id.value),
    ).fetchone()
    if customer is None or (customer["class"], customer["normal_side"], customer["currency"]) != (
        "CUSTOMER_LIABILITY",
        "C",
        "PLN",
    ):
        raise InvariantViolation("Invalid original purchase accounts")
    result = _retained_purchase_result(cur, operation, original)
    if result.http_status != 201:
        raise InvariantViolation("Invalid original purchase outcome")
    _assert_transfer_entries(
        cur,
        original.workspace_id.value,
        UUID(result.body["journal_id"]),
        original.customer_account_id,
        original.merchant_account_id,
        original.amount_minor.value,
    )
    return original


def _authorize_stored(cur, principal, operation):
    if operation is None:
        raise InvariantViolation("Retained request has no operation")
    stored = _parse_stored(operation, principal.workspace_id)
    if type(stored) is PurchaseCommand:
        _authorize_customer(cur, principal, stored)
    elif type(stored) is ReversalCommand:
        _original_purchase(cur, principal, stored)
    elif type(stored) is PayoutCommand:
        _authorize_merchant(cur, principal, stored)
    return stored


def _authorize_merchant(cur, principal, command):
    row = cur.execute(
        "SELECT owner_id,class,normal_side,currency FROM ledger.account "
        "WHERE workspace_id=%s AND id=%s",
        (command.workspace_id.value, command.merchant_account_id.value),
    ).fetchone()
    if row is None or (row["owner_id"], row["class"], row["normal_side"], row["currency"]) != (
        principal.actor_id.value,
        "MERCHANT_LIABILITY",
        "C",
        "PLN",
    ):
        raise NotAuthorized()


def _retained_purchase_result(cur, operation, stored):
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


def _assert_transfer_entries(cur, workspace, journal_id, debit_account, credit_account, amount):
    rows = cur.execute(
        "SELECT line_no,account_id,side,amount_minor FROM ledger.journal_entry "
        "WHERE workspace_id=%s AND journal_id=%s ORDER BY line_no",
        (workspace, journal_id),
    ).fetchall()
    actual = [(row["line_no"], row["account_id"], row["side"], row["amount_minor"]) for row in rows]
    if actual != [(1, debit_account.value, "D", amount), (2, credit_account.value, "C", amount)]:
        raise InvariantViolation("Journal does not match original transfer instruction")


def _retained_reversal_result(cur, operation, stored):
    original = _original_purchase(cur, None, stored)
    attempt = cur.execute(
        "SELECT original_operation_id FROM ledger.reversal_attempt "
        "WHERE workspace_id=%s AND operation_id=%s",
        (stored.workspace_id.value, stored.operation_id.value),
    ).fetchone()
    if attempt is None or attempt["original_operation_id"] != stored.original_operation_id.value:
        raise InvariantViolation("Invalid retained reversal reference")
    relation = cur.execute(
        "SELECT original_operation_id FROM ledger.purchase_reversal_effect "
        "WHERE workspace_id=%s AND reversal_operation_id=%s",
        (stored.workspace_id.value, stored.operation_id.value),
    ).fetchone()
    journal = cur.execute(
        "SELECT journal_id,kind,line_count,total_minor FROM ledger.journal "
        "WHERE workspace_id=%s AND operation_id=%s",
        (stored.workspace_id.value, stored.operation_id.value),
    ).fetchone()
    status, body = operation["http_status"], operation["response_body"]
    if status == 201 and operation["admission"] == "ACCEPTED" and journal is not None:
        expected = _accepted_body(stored, journal["journal_id"], original.amount_minor.value)
        if (
            body == expected
            and type(body.get("contract_version")) is int
            and journal["kind"] == "REVERSAL"
            and journal["line_count"] == 2
            and journal["total_minor"] == original.amount_minor.value
            and relation is not None
            and relation["original_operation_id"] == stored.original_operation_id.value
        ):
            _assert_transfer_entries(
                cur,
                stored.workspace_id.value,
                journal["journal_id"],
                original.merchant_account_id,
                original.customer_account_id,
                original.amount_minor.value,
            )
            return AdmissionResult(status, body, replayed=True)
    if (
        status == 422
        and operation["admission"] == "REJECTED"
        and journal is None
        and relation is None
    ):
        error = body.get("error", {})
        try:
            request_id = str(UUID(error["request_id"]))
            expected = _rejection(stored, error["code"], request_id).body
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        else:
            if body == expected and error.get("retryable") is False:
                if (
                    error["code"] == "ALREADY_REVERSED"
                    and cur.execute(
                        "SELECT 1 FROM ledger.purchase_reversal_effect "
                        "WHERE workspace_id=%s AND original_operation_id=%s "
                        "AND reversal_operation_id<>%s",
                        (
                            stored.workspace_id.value,
                            stored.original_operation_id.value,
                            stored.operation_id.value,
                        ),
                    ).fetchone()
                    is None
                ):
                    raise InvariantViolation(
                        "Retained already-reversed result has no successful reversal"
                    )
                return AdmissionResult(status, body, replayed=True)
    raise InvariantViolation("Invalid retained reversal outcome")


def _retained_result(cur, operation, stored):
    if type(stored) is PurchaseCommand:
        return _retained_purchase_result(cur, operation, stored)
    if type(stored) is ReversalCommand:
        return _retained_reversal_result(cur, operation, stored)
    if type(stored) is PayoutCommand:
        from ledgerops.db.payout_admission import retained_payout_result

        return retained_payout_result(cur, operation, stored)
    raise InvariantViolation("Unsupported retained operation kind")


def _rejection(command, code, request_id):
    messages = {
        "INSUFFICIENT_AVAILABLE": "Insufficient available balance",
        "LIMIT_EXCEEDED": "Purchase would exceed ledger limits",
    }
    if type(command) is ReversalCommand:
        messages.update(
            LIMIT_EXCEEDED="Reversal would exceed ledger limits",
            ALREADY_REVERSED="Purchase already reversed",
        )
    elif type(command) is PayoutCommand:
        messages["LIMIT_EXCEEDED"] = "Payout would exceed ledger limits"
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


def _open_admission(cur, principal, command, authorization_guard=None):
    """Set options, enter the barrier, then apply the command's ownership policy."""
    workspace = command.workspace_id.value
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
    cur.execute("SELECT set_config('lock_timeout', '5s', true)")
    cur.execute("SELECT set_config('statement_timeout', '10s', true)")
    cur.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        (f"ledgerops:workspace:{command.workspace_id}",),
    )
    ws = cur.execute("SELECT mode FROM ledger.workspace WHERE id = %s", (workspace,)).fetchone()
    if ws is None:
        raise NotAuthorized()
    if ws["mode"] != "NORMAL":
        raise RetrySameIdentity("workspace_not_normal")
    if authorization_guard is not None:
        scopes = {
            PurchaseCommand: "purchase:write",
            ReversalCommand: "reversal:write",
            PayoutCommand: "payout:write",
        }
        authorization_guard(cur, principal, scopes[type(command)])
    if type(command) is PurchaseCommand:
        _authorize_customer(cur, principal, command)
    elif type(command) is ReversalCommand:
        _original_purchase(cur, principal, command)
    elif type(command) is PayoutCommand:
        _authorize_merchant(cur, principal, command)
    else:
        raise TypeError("unsupported business command")


def _replay_by_request_key(cur, principal, scope, payload):
    """Authorize and return the retained result for an already claimed request key."""
    workspace = scope[0]
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
    if any(retained[field] != op[field] for field in ("payload", "http_status")) or json.dumps(
        retained["response_body"], sort_keys=True
    ) != json.dumps(op["response_body"], sort_keys=True):
        raise InvariantViolation("Request and operation disagree")
    result = _retained_result(cur, op, stored)
    return result


def _replay_by_operation(cur, principal, scope, command, payload):
    """Authorize the retained business result and link the newly claimed request key."""
    workspace = command.workspace_id.value
    operation_id = command.operation_id.value
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
    return result


def _lock_balances(cur, workspace, customer_account, merchant_account):
    """Take the same account UUID order for either direction of transfer."""
    balances = {}
    for account in sorted(
        (customer_account, merchant_account), key=lambda account: account.value.int
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
        if not row["enabled"] or row["currency"] != "PLN" or row["normal_side"] != "C":
            raise NotAuthorized()
        balances[account] = row
    if (
        balances[customer_account]["class"] != "CUSTOMER_LIABILITY"
        or balances[merchant_account]["class"] != "MERCHANT_LIABILITY"
    ):
        raise NotAuthorized()
    return balances


def _accepted_body(command, journal_id, amount):
    body = {
        "operation_id": str(command.operation_id),
        "admission": "accepted",
        "journal_id": str(journal_id),
        "amount_minor": str(amount),
        "currency": "PLN",
        "effect": "purchase_posted" if type(command) is PurchaseCommand else "purchase_reversed",
        "contract_version": 1,
    }
    if type(command) is ReversalCommand:
        body["original_operation_id"] = str(command.original_operation_id)
    return body


def _post_transfer(cur, command, balances, debit_account, credit_account, amount):
    """One journal/delta primitive; caller supplies the two concrete business directions."""
    workspace, operation_id = command.workspace_id.value, command.operation_id.value
    journal_id = uuid4()
    kind = "PURCHASE" if type(command) is PurchaseCommand else "REVERSAL"
    cur.execute(
        """INSERT INTO ledger.journal
           (workspace_id,journal_id,operation_id,kind,line_count,total_minor)
           VALUES (%s,%s,%s,%s,2,%s)""",
        (workspace, journal_id, operation_id, kind, amount),
    )
    cur.execute(
        """INSERT INTO ledger.journal_entry
           (workspace_id,journal_id,line_no,account_id,side,amount_minor)
           VALUES (%s,%s,1,%s,'D',%s), (%s,%s,2,%s,'C',%s)""",
        (
            workspace,
            journal_id,
            debit_account.value,
            amount,
            workspace,
            journal_id,
            credit_account.value,
            amount,
        ),
    )
    for account in sorted(balances, key=lambda account: account.value.int):
        delta = (amount, 0) if account == debit_account else (0, amount)
        updated = cur.execute(
            """UPDATE ledger.account_balance
               SET debits=debits+%s, credits=credits+%s, version=version+1
               WHERE workspace_id=%s AND account_id=%s RETURNING 1""",
            (*delta, workspace, account.value),
        ).fetchone()
        if updated is None:
            raise InvariantViolation("Locked account projection disappeared")
    return AdmissionResult(201, _accepted_body(command, journal_id, amount))


def _transfer_rejection(balances, debit_account, credit_account, amount):
    debit, credit = balances[debit_account], balances[credit_account]
    if int(debit["booked"]) - int(debit["reserved"]) < amount:
        return "INSUFFICIENT_AVAILABLE"
    # Convert exact values before arithmetic; Decimal's ambient precision must
    # not round 38-digit cumulative totals.
    if (
        int(debit["debits"]) + amount > MAX_TOTAL_MINOR
        or int(credit["credits"]) + amount > MAX_TOTAL_MINOR
        or any(row["version"] >= 2**63 - 1 for row in balances.values())
    ):
        return "LIMIT_EXCEEDED"
    return None


def _finish_operation(cur, command, scope, result):
    updated = cur.execute(
        """UPDATE ledger.operation
           SET admission=%s, http_status=%s, response_body=%s
           WHERE workspace_id=%s AND operation_id=%s RETURNING 1""",
        (
            "ACCEPTED" if result.http_status in (201, 202) else "REJECTED",
            result.http_status,
            Jsonb(result.body),
            command.workspace_id.value,
            command.operation_id.value,
        ),
    ).fetchone()
    if updated is None:
        raise InvariantViolation("Operation claim disappeared")
    _finish_request(cur, scope, result)
    return result


def _post_new_purchase(cur, principal, command, scope):
    balances = _lock_balances(
        cur, command.workspace_id.value, command.customer_account_id, command.merchant_account_id
    )
    if balances[command.customer_account_id]["owner_id"] != principal.actor_id.value:
        raise NotAuthorized()
    code = _transfer_rejection(
        balances,
        command.customer_account_id,
        command.merchant_account_id,
        command.amount_minor.value,
    )
    result = (
        _rejection(command, code, str(uuid4()))
        if code
        else _post_transfer(
            cur,
            command,
            balances,
            command.customer_account_id,
            command.merchant_account_id,
            command.amount_minor.value,
        )
    )
    return _finish_operation(cur, command, scope, result)


def _post_new_reversal(cur, principal, command, scope):
    """Serialize an original before balances; only a successful effect owns uniqueness."""
    original = _original_purchase(cur, principal, command, lock=True)
    workspace = command.workspace_id.value
    cur.execute(
        "INSERT INTO ledger.reversal_attempt (workspace_id,operation_id,original_operation_id) "
        "VALUES (%s,%s,%s)",
        (workspace, command.operation_id.value, command.original_operation_id.value),
    )
    existing = cur.execute(
        "SELECT reversal_operation_id FROM ledger.purchase_reversal_effect "
        "WHERE workspace_id=%s AND original_operation_id=%s",
        (workspace, command.original_operation_id.value),
    ).fetchone()
    balances = _lock_balances(
        cur, workspace, original.customer_account_id, original.merchant_account_id
    )
    if balances[original.merchant_account_id]["owner_id"] != principal.actor_id.value:
        raise NotAuthorized()
    code = (
        "ALREADY_REVERSED"
        if existing is not None
        else _transfer_rejection(
            balances,
            original.merchant_account_id,
            original.customer_account_id,
            original.amount_minor.value,
        )
    )
    if code:
        result = _rejection(command, code, str(uuid4()))
    else:
        result = _post_transfer(
            cur,
            command,
            balances,
            original.merchant_account_id,
            original.customer_account_id,
            original.amount_minor.value,
        )
        # Do not swallow a uniqueness violation: it means original serialization
        # or another stated invariant was bypassed, not an ordinary replay.
        cur.execute(
            "INSERT INTO ledger.purchase_reversal_effect "
            "(workspace_id,original_operation_id,reversal_operation_id) VALUES (%s,%s,%s)",
            (workspace, command.original_operation_id.value, command.operation_id.value),
        )
    return _finish_operation(cur, command, scope, result)


def _submit_once(
    conn: psycopg.Connection,
    principal: Principal,
    command,
    request_key: RequestKey,
    command_type,
    *,
    post_new=None,
    authorization_guard=None,
) -> AdmissionResult:
    """One complete attempt; return only after COMMIT and deferred checks.

    Authorization/identity errors roll back claims. Insufficient funds and bounded
    total/version exhaustion are committed business outcomes. The public entry point
    may retry a definitely aborted transaction; the caller still owns the connection.
    """
    commands = {
        PurchaseCommand: ("PURCHASE", PURCHASE_ROUTE, _post_new_purchase),
        ReversalCommand: ("REVERSAL", REVERSAL_ROUTE, _post_new_reversal),
        PayoutCommand: ("PAYOUT", PAYOUT_ROUTE, post_new),
    }
    if command_type not in commands:
        raise TypeError("unsupported business command")
    kind, route, post = commands[command_type]
    if (
        type(principal) is not Principal
        or type(command) is not command_type
        or type(request_key) is not RequestKey
    ):
        raise TypeError(f"{kind.lower()} requires typed principal, command and request key")
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError(f"{kind.lower()} admission requires an idle autocommit connection")
    if (
        command.workspace_id != principal.workspace_id
        or request_key.workspace_id != principal.workspace_id
        or request_key.client_id != principal.client_id
    ):
        raise NotAuthorized()
    if request_key.route_family != route:
        raise ValueError("request route family must match the business command")
    workspace = command.workspace_id.value
    operation_id = command.operation_id.value
    payload, digest = command.canonical_bytes, command.payload_sha256
    scope = (workspace, principal.client_id.value, request_key.route_family, request_key.key)

    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            _open_admission(cur, principal, command, authorization_guard)

            claimed_request = cur.execute(
                """INSERT INTO ledger.request_result
                   (workspace_id, client_id, route_family, key,
                    operation_id, payload, payload_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT ON CONSTRAINT request_result_pkey DO NOTHING RETURNING 1""",
                (*scope, operation_id, payload, digest),
            ).fetchone()
            if claimed_request is None:
                result = _replay_by_request_key(cur, principal, scope, payload)
            else:
                claimed_operation = cur.execute(
                    """INSERT INTO ledger.operation
                       (workspace_id, operation_id, kind, payload, payload_sha256)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT ON CONSTRAINT operation_pkey DO NOTHING RETURNING 1""",
                    (workspace, operation_id, kind, payload, digest),
                ).fetchone()
                if claimed_operation is None:
                    result = _replay_by_operation(cur, principal, scope, command, payload)
                else:
                    if post is None:
                        raise TypeError("business admission handler required")
                    result = post(cur, principal, command, scope)
        return result
    except (
        psycopg.errors.LockNotAvailable,
        psycopg.errors.QueryCanceled,
    ) as exc:
        raise RetrySameIdentity("database_contention") from exc


def _submit_purchase_once(conn, principal, command, request_key):
    """Keep the R1 entry point while sharing the actual transaction body."""
    return _submit_once(conn, principal, command, request_key, PurchaseCommand)


def _submit_reversal_once(conn, principal, command, request_key):
    return _submit_once(conn, principal, command, request_key, ReversalCommand)


def _submit_with_retry(conn, principal, command, request_key, submit_once):
    """Retry only definitely aborted 40P01/40001 transactions, retaining input identity."""
    for attempt in range(1, 4):
        try:
            return submit_once(conn, principal, command, request_key)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure) as exc:
            if conn.closed or conn.info.transaction_status != TransactionStatus.IDLE:
                raise
            if attempt == 3:
                raise RetrySameIdentity("transaction_retries_exhausted") from exc
            sleep(uniform(0.01, 0.05) * attempt)
    raise AssertionError("unreachable retry state")


def submit_purchase(
    conn: psycopg.Connection,
    principal: Principal,
    command: PurchaseCommand,
    request_key: RequestKey,
    *,
    authorization_guard=None,
) -> AdmissionResult:
    """Admit/replay a purchase; retry at most three whole definitely aborted transactions.

    Timeout/cancellation and integrity faults are not internally retried. Connection
    loss, including COMMIT, propagates: reconnect and resubmit the original identities.
    """
    submit_once = (
        _submit_purchase_once
        if authorization_guard is None
        else (
            lambda c, p, cmd, key: _submit_once(
                c, p, cmd, key, PurchaseCommand, authorization_guard=authorization_guard
            )
        )
    )
    return _submit_with_retry(conn, principal, command, request_key, submit_once)


def submit_reversal(
    conn: psycopg.Connection,
    principal: Principal,
    command: ReversalCommand,
    request_key: RequestKey,
    *,
    authorization_guard=None,
) -> AdmissionResult:
    """Original merchant's full reversal through the same admission/retry boundaries."""
    submit_once = (
        _submit_reversal_once
        if authorization_guard is None
        else (
            lambda c, p, cmd, key: _submit_once(
                c, p, cmd, key, ReversalCommand, authorization_guard=authorization_guard
            )
        )
    )
    return _submit_with_retry(conn, principal, command, request_key, submit_once)


def submit_payout(
    conn, principal, command, request_key, *, policy, clock=None, authorization_guard=None
):
    """Reserve funds and retain a 202 receipt; no external I/O or payout settlement."""
    from ledgerops.db.payout_admission import payout_submitter

    return _submit_with_retry(
        conn,
        principal,
        command,
        request_key,
        payout_submitter(policy, clock, authorization_guard=authorization_guard),
    )


def get_payout_state(conn, principal, payout_id):
    """Read current progress through the original merchant's ownership boundary."""
    from ledgerops.db.payout_admission import payout_state

    return payout_state(conn, principal, payout_id)
