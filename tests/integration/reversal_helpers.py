"""Real reversal inputs and raw observations; expected outcomes live in the tests."""

from uuid import uuid4

from ledgerops.db.admission import Principal, submit_reversal
from ledgerops.domain import (
    ActorId,
    ClientId,
    OperationId,
    RequestKey,
    ReversalCommand,
    WorkspaceId,
)
from tests.integration.helpers import fetch_ledger


def reversal_input(
    golden,
    *,
    operation_id=None,
    original_operation_id=None,
    actor_id=None,
    client_id="60000000-0000-4000-8000-000000000002",
    key="reverse-purchase-80",
    reason="customer_refund",
):
    workspace = WorkspaceId.parse(golden["workspace"]["id"])
    merchant = next(
        a
        for a in golden["accounts"]
        if a["id"] == golden["purchase_request"]["merchant_account_id"]
    )
    principal = Principal(
        workspace,
        ClientId.parse(client_id),
        ActorId.parse(actor_id or merchant["owner_id"]),
    )
    return (
        principal,
        ReversalCommand(
            workspace,
            OperationId.parse(operation_id or str(uuid4())),
            OperationId.parse(original_operation_id or golden["purchase_request"]["operation_id"]),
            reason,
        ),
        RequestKey(workspace, principal.client_id, key, "reversal:v1"),
    )


def reverse(db, *arguments):
    with db.connect("app") as conn:
        return submit_reversal(conn, *arguments)


def reversal_state(db):
    """Whole retained financial state, including both new reversal relations."""
    tables = {
        "operation": "workspace_id,operation_id",
        "request_result": "workspace_id,client_id,route_family,key",
        "journal": "workspace_id,journal_id",
        "journal_entry": "workspace_id,journal_id,line_no",
        "account_balance": "workspace_id,account_id",
        "reversal_attempt": "workspace_id,operation_id",
        "purchase_reversal_effect": "workspace_id,original_operation_id",
    }
    with db.connect("app") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
            for table, order in tables.items()
        }


def original_rows(db, operation_id):
    """Retain the exact original purchase bytes/rows before any reversal action."""
    with db.connect("app") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            "operation": conn.execute(
                "SELECT * FROM ledger.operation WHERE operation_id=%s", (operation_id,)
            ).fetchall(),
            "journal": conn.execute(
                "SELECT * FROM ledger.journal WHERE operation_id=%s", (operation_id,)
            ).fetchall(),
            "entries": conn.execute(
                "SELECT e.* FROM ledger.journal_entry e JOIN ledger.journal j "
                "ON j.workspace_id=e.workspace_id AND j.journal_id=e.journal_id "
                "WHERE j.operation_id=%s ORDER BY e.line_no",
                (operation_id,),
            ).fetchall(),
        }


def assert_balances(db, golden, amounts):
    from reference.journal_fold import assert_projection_matches

    raw = fetch_ledger(db)
    assert_projection_matches(raw["accounts"], raw["entries"], raw["projections"])
    balances = {str(r["account_id"]): r for r in raw["projections"]}
    assert [balances[a["id"]]["booked"] for a in golden["accounts"]] == amounts


def assert_retained(db, arguments, result):
    principal, command, key = arguments
    with db.connect("app") as conn:
        assert conn.execute(
            "SELECT admission,http_status,response_body FROM ledger.operation "
            "WHERE workspace_id=%s AND operation_id=%s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone() == (
            "ACCEPTED" if result.http_status == 201 else "REJECTED",
            result.http_status,
            result.body,
        )
        assert conn.execute(
            "SELECT operation_id,http_status,response_body FROM ledger.request_result "
            "WHERE workspace_id=%s AND client_id=%s AND route_family=%s AND key=%s",
            (str(command.workspace_id), str(principal.client_id), key.route_family, key.key),
        ).fetchone() == (command.operation_id.value, result.http_status, result.body)
