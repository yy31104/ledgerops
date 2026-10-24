"""Explicit fixture SQL only: these helpers are not production admission code."""

import copy
import hashlib
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def account_id(golden, label):
    return next(row["id"] for row in golden["accounts"] if row["label"] == label)


def seed_accounts(db, golden):
    """Privileged setup creates identities and *zero* projections, never opening money."""
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute(
            "INSERT INTO ledger.workspace (id, currency) VALUES (%s, %s)",
            (golden["workspace"]["id"], golden["workspace"]["currency"]),
        )
        for account in golden["accounts"]:
            conn.execute(
                """INSERT INTO ledger.account
                   (workspace_id, id, owner_id, class, normal_side, currency)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                tuple(
                    account[key]
                    for key in (
                        "workspace_id",
                        "id",
                        "owner_id",
                        "class",
                        "normal_side",
                        "currency",
                    )
                ),
            )
            conn.execute(
                """INSERT INTO ledger.account_balance (workspace_id, account_id, normal_side)
                   VALUES (%s, %s, %s)""",
                (account["workspace_id"], account["id"], account["normal_side"]),
            )


def insert_operation(
    conn,
    workspace_id,
    operation_id=None,
    *,
    kind="PURCHASE",
    pending=False,
    payload=b"explicit fixture operation",
    admission="ACCEPTED",
):
    operation_id = str(operation_id or uuid4())
    status = None if pending else (201 if admission == "ACCEPTED" else 422)
    body = None if pending else Jsonb({"operation_id": operation_id, "fixture": True})
    conn.execute(
        """INSERT INTO ledger.operation
           (workspace_id, operation_id, kind, payload, payload_sha256,
            admission, http_status, response_body)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            workspace_id,
            operation_id,
            kind,
            payload,
            hashlib.sha256(payload).digest(),
            "IN_PROGRESS" if pending else admission,
            status,
            body,
        ),
    )
    return operation_id


def insert_request(
    conn,
    workspace_id,
    operation_id,
    *,
    pending=False,
    client_id=None,
    key=None,
    payload=b"explicit fixture request",
    status=201,
):
    client_id = str(client_id or uuid4())
    key = str(uuid4()) if key is None else key
    conn.execute(
        """INSERT INTO ledger.request_result
           (workspace_id, client_id, route_family, key, operation_id,
            payload, payload_sha256, http_status, response_body)
           VALUES (%s, %s, 'purchase:v1', %s, %s, %s, %s, %s, %s)""",
        (
            workspace_id,
            client_id,
            key,
            operation_id,
            payload,
            hashlib.sha256(payload).digest(),
            None if pending else status,
            None if pending else Jsonb({"operation_id": str(operation_id), "fixture": True}),
        ),
    )
    return client_id, key


def insert_header(conn, journal):
    conn.execute(
        """INSERT INTO ledger.journal
           (workspace_id, journal_id, operation_id, kind, line_count, total_minor)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        tuple(
            journal[key]
            for key in (
                "workspace_id",
                "journal_id",
                "operation_id",
                "kind",
                "line_count",
                "total_minor",
            )
        ),
    )


def insert_entries(conn, entries):
    for entry in entries:
        conn.execute(
            """INSERT INTO ledger.journal_entry
               (workspace_id, journal_id, line_no, account_id, side, amount_minor)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            tuple(
                entry[key]
                for key in (
                    "workspace_id",
                    "journal_id",
                    "line_no",
                    "account_id",
                    "side",
                    "amount_minor",
                )
            ),
        )


def write_journal(
    conn,
    golden,
    kind="OPENING",
    *,
    omit_projection_account_id=None,
    credit_account_id=None,
):
    """Write a frozen scenario inside the caller's transaction; no business decisions."""
    journal = copy.deepcopy(next(row for row in golden["journals"] if row["kind"] == kind))
    entries = copy.deepcopy(
        [row for row in golden["entries"] if row["journal_id"] == journal["journal_id"]]
    )
    if credit_account_id is not None:
        for entry in entries:
            if entry["side"] == "C":
                entry["account_id"] = credit_account_id
    insert_operation(conn, journal["workspace_id"], journal["operation_id"], kind=kind)
    insert_header(conn, journal)
    insert_entries(conn, entries)
    for entry in entries:
        if entry["account_id"] == omit_projection_account_id:
            continue
        conn.execute(
            """UPDATE ledger.account_balance
               SET debits = debits + %s, credits = credits + %s, version = version + 1
               WHERE workspace_id = %s AND account_id = %s""",
            (
                entry["amount_minor"] if entry["side"] == "D" else 0,
                entry["amount_minor"] if entry["side"] == "C" else 0,
                entry["workspace_id"],
                entry["account_id"],
            ),
        )
    return journal


def post_fixture(db, golden, kind="OPENING", **defects):
    """The runtime credential can execute valid fixture SQL in one explicit transaction."""
    with db.connect(role="app") as conn, conn.transaction():
        return write_journal(conn, golden, kind, **defects)


def fetch_ledger(db):
    """Raw consistent snapshot; no production posting, money, or oracle helpers."""
    tables = {
        "accounts": "account",
        "journals": "journal",
        "entries": "journal_entry",
        "projections": "account_balance",
    }
    snapshot = {}
    with db.connect(role="app") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with conn.cursor(row_factory=dict_row) as cur:
            for name, table in tables.items():
                # Names come only from the constant mapping above.
                rows = cur.execute(f"SELECT * FROM ledger.{table}").fetchall()
                snapshot[name] = [
                    {
                        key: str(value) if isinstance(value, UUID) else value
                        for key, value in row.items()
                    }
                    for row in rows
                ]
    return snapshot
