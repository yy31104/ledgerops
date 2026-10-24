import copy
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from tests.integration.helpers import (
    account_id,
    insert_entries,
    insert_header,
    insert_operation,
    insert_request,
    seed_accounts,
)

pytestmark = pytest.mark.integration


def test_numeric_totals_reject_noninteger_nonfinite_and_overflow(seeded_db, golden):
    wid = golden["workspace"]["id"]
    merchant = account_id(golden, "M2")
    bad_values = ["NaN", "Infinity", "-Infinity", "0.5", "-1", "1e38"]
    with seeded_db.connect() as conn:
        for column in ("debits", "credits", "reserved"):
            for value in bad_values:
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(
                        f"UPDATE ledger.account_balance SET {column} = %s "
                        "WHERE workspace_id = %s AND account_id = %s",
                        (Decimal(value), wid, merchant),
                    )
        maximum = 10**38 - 1
        conn.execute(
            """UPDATE ledger.account_balance SET debits = %s, credits = %s
               WHERE workspace_id = %s AND account_id = %s""",
            (maximum, maximum, wid, merchant),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """UPDATE ledger.account_balance SET credits = credits + 1
                   WHERE workspace_id = %s AND account_id = %s""",
                (wid, merchant),
            )


def test_projection_row_bounds_and_generated_booked(seeded_db, golden):
    wid = golden["workspace"]["id"]
    customer = account_id(golden, "customer")
    with seeded_db.connect() as conn:
        conn.execute(
            """UPDATE ledger.account_balance SET debits = debits + 80, version = version + 1
               WHERE workspace_id = %s AND account_id = %s""",
            (wid, customer),
        )
        row = conn.execute(
            """SELECT booked, reserved, version FROM ledger.account_balance
               WHERE workspace_id = %s AND account_id = %s""",
            (wid, customer),
        ).fetchone()
        assert row == (20, 0, 2)
        for assignment in ("reserved = 21", "debits = 101", "version = -1"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    f"UPDATE ledger.account_balance SET {assignment} "
                    "WHERE workspace_id = %s AND account_id = %s",
                    (wid, customer),
                )
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                """UPDATE ledger.account_balance SET credits = NULL
                   WHERE workspace_id = %s AND account_id = %s""",
                (wid, customer),
            )


@pytest.mark.parametrize("amount", [0, -1, 9_000_000_000_000_001, 2**63])
def test_entry_amount_bounds_on_real_database(db, golden, amount):
    seed_accounts(db, golden)
    journal = copy.deepcopy(golden["journals"][0])
    entry = copy.deepcopy(golden["entries"][0])
    entry["amount_minor"] = amount
    error = (
        psycopg.errors.NumericValueOutOfRange if amount == 2**63 else psycopg.errors.CheckViolation
    )
    with db.connect() as conn, pytest.raises(error), conn.transaction():
        insert_operation(conn, journal["workspace_id"], journal["operation_id"], kind="OPENING")
        insert_header(conn, journal)
        insert_entries(conn, [entry])


def test_maximum_entry_amount_can_commit(db, golden):
    seed_accounts(db, golden)
    maximum = 9_000_000_000_000_000
    journal = copy.deepcopy(golden["journals"][0])
    journal["total_minor"] = maximum
    entries = copy.deepcopy(golden["entries"][:2])
    for entry in entries:
        entry["amount_minor"] = maximum
    with db.connect() as conn, conn.transaction():
        insert_operation(conn, journal["workspace_id"], journal["operation_id"], kind="OPENING")
        insert_header(conn, journal)
        insert_entries(conn, entries)


def test_scope_foreign_keys_refuse_cross_workspace_relations(seeded_db, golden):
    wid = golden["workspace"]["id"]
    other = str(uuid4())
    foreign_account = str(uuid4())
    with seeded_db.connect(role="owner") as conn:
        conn.execute("INSERT INTO ledger.workspace (id) VALUES (%s)", (other,))
        conn.execute(
            """INSERT INTO ledger.account (workspace_id, id, owner_id, class, normal_side)
               VALUES (%s, %s, %s, 'MERCHANT_LIABILITY', 'C')""",
            (other, foreign_account, str(uuid4())),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """INSERT INTO ledger.account_balance (workspace_id, account_id, normal_side)
                   VALUES (%s, %s, 'C')""",
                (wid, foreign_account),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """INSERT INTO ledger.account_balance (workspace_id, account_id, normal_side)
                   VALUES (%s, %s, 'D')""",
                (other, foreign_account),
            )
    opening = golden["journals"][0]
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert_entries(
                conn,
                [
                    {
                        "workspace_id": wid,
                        "journal_id": opening["journal_id"],
                        "line_no": 3,
                        "account_id": foreign_account,
                        "side": "C",
                        "amount_minor": 1,
                    }
                ],
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            insert_request(conn, other, opening["operation_id"])
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            wrong_header = {**opening, "workspace_id": other, "journal_id": str(uuid4())}
            insert_header(conn, wrong_header)


def test_account_identity_is_immutable_even_with_owner_setup_connection(seeded_db, golden):
    wid = golden["workspace"]["id"]
    customer = account_id(golden, "customer")
    with seeded_db.connect(role="owner") as conn:
        for column, value in (
            ("owner_id", str(uuid4())),
            ("normal_side", "D"),
            ("class", "MERCHANT_LIABILITY"),
            ("currency", "EUR"),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    f"UPDATE ledger.account SET {column} = %s WHERE workspace_id = %s AND id = %s",
                    (value, wid, customer),
                )


def test_payload_hash_is_checked_and_request_key_is_byte_bounded(seeded_db, golden):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO ledger.operation
                   (workspace_id, operation_id, kind, payload, payload_sha256,
                    admission, http_status, response_body)
                   VALUES (%s, %s, 'PURCHASE', 'x'::bytea, 'wrong'::bytea,
                           'ACCEPTED', 201, '{}'::jsonb)""",
                (wid, str(uuid4())),
            )
        operation = insert_operation(conn, wid)
        insert_request(conn, wid, operation, key="é" * 64)
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_request(conn, wid, operation, key="é" * 65)
