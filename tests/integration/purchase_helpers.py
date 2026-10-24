"""Real-service inputs plus explicit privileged fixture changes, never an oracle."""

from uuid import uuid4

from ledgerops.db.admission import Principal, submit_purchase
from ledgerops.domain import ActorId, ClientId, PurchaseCommand, RequestKey, WorkspaceId
from tests.integration.helpers import insert_entries, insert_header, insert_operation


def purchase_input(
    golden,
    *,
    key="purchase-80",
    client_id="60000000-0000-4000-8000-000000000001",
    actor_id=None,
    **payload_changes,
):
    frozen = golden["purchase_request"]
    workspace = WorkspaceId.parse(golden["workspace"]["id"])
    principal = Principal(
        workspace_id=workspace,
        client_id=ClientId.parse(client_id),
        actor_id=ActorId.parse(actor_id or frozen["customer_id"]),
    )
    payload = {
        "operation_id": frozen["operation_id"],
        "customer_account_id": frozen["customer_account_id"],
        "merchant_account_id": frozen["merchant_account_id"],
        "amount_minor": str(frozen["amount_minor"]),
        "currency": frozen["currency"],
        **payload_changes,
    }
    return (
        principal,
        PurchaseCommand.from_payload(workspace, payload),
        RequestKey(workspace, principal.client_id, key),
    )


def invoke(db, principal, command, request_key):
    with db.connect(role="app") as conn:
        return submit_purchase(conn, principal, command, request_key)


def retained_rows(db):
    """Deterministically capture business tables to detect any unexpected mutation."""
    with db.connect(role="app") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        primary_keys = {
            "operation": "workspace_id,operation_id",
            "request_result": "workspace_id,client_id,route_family,key",
            "journal": "workspace_id,journal_id",
            "journal_entry": "workspace_id,journal_id,line_no",
            "account_balance": "workspace_id,account_id",
        }
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {ordering}").fetchall()
            for table, ordering in primary_keys.items()
        }


def fund_customer(db, golden, amount, *, customer_account_id=None):
    """Explicit setup OPENING journal, not purchase admission or production logic."""
    workspace = golden["workspace"]["id"]
    clearing = golden["accounts"][0]["id"]
    customer = customer_account_id or golden["purchase_request"]["customer_account_id"]
    operation_id, journal_id = str(uuid4()), str(uuid4())
    with db.connect(role="app") as conn, conn.transaction():
        insert_operation(conn, workspace, operation_id, kind="OPENING")
        insert_header(
            conn,
            {
                "workspace_id": workspace,
                "journal_id": journal_id,
                "operation_id": operation_id,
                "kind": "OPENING",
                "line_count": 2,
                "total_minor": amount,
            },
        )
        insert_entries(
            conn,
            [
                {
                    "workspace_id": workspace,
                    "journal_id": journal_id,
                    "line_no": 1,
                    "account_id": clearing,
                    "side": "D",
                    "amount_minor": amount,
                },
                {
                    "workspace_id": workspace,
                    "journal_id": journal_id,
                    "line_no": 2,
                    "account_id": customer,
                    "side": "C",
                    "amount_minor": amount,
                },
            ],
        )
        conn.execute(
            """UPDATE ledger.account_balance
               SET debits = debits + %s, version = version + 1
               WHERE workspace_id = %s AND account_id = %s""",
            (amount, workspace, clearing),
        )
        conn.execute(
            """UPDATE ledger.account_balance
               SET credits = credits + %s, version = version + 1
               WHERE workspace_id = %s AND account_id = %s""",
            (amount, workspace, customer),
        )


def add_customer(db, golden):
    """Privileged setup creates a second identity with a zero projection."""
    account_id, actor_id = str(uuid4()), str(uuid4())
    workspace = golden["workspace"]["id"]
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute(
            """INSERT INTO ledger.account
               (workspace_id,id,owner_id,class,normal_side,currency)
               VALUES (%s,%s,%s,'CUSTOMER_LIABILITY','C','PLN')""",
            (workspace, account_id, actor_id),
        )
        conn.execute(
            """INSERT INTO ledger.account_balance (workspace_id,account_id,normal_side)
               VALUES (%s,%s,'C')""",
            (workspace, account_id),
        )
    return account_id, actor_id
