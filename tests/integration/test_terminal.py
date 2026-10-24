from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.integration.helpers import insert_operation, insert_request

pytestmark = pytest.mark.integration


def test_deferred_checks_read_final_rows_and_request_may_precede_operation(seeded_db, golden):
    wid = golden["workspace"]["id"]
    operation = str(uuid4())
    with seeded_db.connect() as conn:
        with conn.transaction():
            client, key = insert_request(conn, wid, operation, pending=True)
            insert_operation(conn, wid, operation, pending=True)
            body = Jsonb({"operation_id": operation, "effect": "fixture"})
            conn.execute(
                """UPDATE ledger.operation SET admission = 'ACCEPTED', http_status = 201,
                   response_body = %s WHERE workspace_id = %s AND operation_id = %s""",
                (body, wid, operation),
            )
            conn.execute(
                """UPDATE ledger.request_result SET http_status = 201, response_body = %s
                   WHERE workspace_id = %s AND client_id = %s AND key = %s""",
                (body, wid, client, key),
            )
        assert conn.execute(
            """SELECT admission, http_status FROM ledger.operation
               WHERE workspace_id = %s AND operation_id = %s""",
            (wid, operation),
        ).fetchone() == ("ACCEPTED", 201)
        assert conn.execute(
            "SELECT http_status FROM ledger.request_result WHERE client_id = %s AND key = %s",
            (client, key),
        ).fetchone() == (201,)


def test_pending_operation_cannot_survive_commit(seeded_db, golden):
    wid = golden["workspace"]["id"]
    operation = str(uuid4())
    reached_commit = False
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="operation is incomplete"):
            with conn.transaction():
                insert_operation(conn, wid, operation, pending=True)
                reached_commit = True
        assert reached_commit
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE operation_id = %s",
            (operation,),
        ).fetchone() == (0,)


def test_pending_request_cannot_survive_commit(seeded_db, golden):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        operation = insert_operation(conn, wid)
        with pytest.raises(psycopg.errors.CheckViolation, match="request result is incomplete"):
            with conn.transaction():
                insert_request(conn, wid, operation, pending=True)
        assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE operation_id = %s",
            (operation,),
        ).fetchone() == (1,)


def test_terminal_results_are_immutable_including_noop_updates(seeded_db, golden):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        operation = insert_operation(conn, wid, admission="REJECTED")
        client, key = insert_request(conn, wid, operation, status=422)
        for assignment in (
            "admission = admission",
            "http_status = http_status",
            "response_body = response_body",
            "admission = 'ACCEPTED', http_status = 201",
            "response_body = '{\"changed\":true}'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="one transition to terminal"):
                conn.execute(
                    f"UPDATE ledger.operation SET {assignment} WHERE operation_id = %s",
                    (operation,),
                )
        for assignment in (
            "http_status = http_status",
            "response_body = response_body",
            "http_status = 201",
            "response_body = '{\"changed\":true}'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="one transition to terminal"):
                conn.execute(
                    f"UPDATE ledger.request_result SET {assignment} "
                    "WHERE client_id = %s AND key = %s",
                    (client, key),
                )
        assert conn.execute(
            "SELECT admission, http_status FROM ledger.operation WHERE operation_id = %s",
            (operation,),
        ).fetchone() == ("REJECTED", 422)
        assert conn.execute(
            "SELECT http_status FROM ledger.request_result WHERE client_id = %s AND key = %s",
            (client, key),
        ).fetchone() == (422,)


@pytest.mark.parametrize(
    ("admission", "status", "body"),
    [
        ("ACCEPTED", None, Jsonb({})),
        ("ACCEPTED", 201, None),
        ("ACCEPTED", 201, Jsonb(None)),
        ("ACCEPTED", 201, Jsonb([])),
        ("ACCEPTED", 422, Jsonb({})),
        ("REJECTED", 201, Jsonb({})),
        ("IN_PROGRESS", 201, Jsonb({})),
        ("UNKNOWN", None, None),
    ],
)
def test_operation_status_body_coherence_is_enforced(seeded_db, golden, admission, status, body):
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO ledger.operation
                   (workspace_id, operation_id, kind, payload, payload_sha256,
                    admission, http_status, response_body)
                   VALUES (%s, %s, 'PURCHASE', 'x'::bytea, sha256('x'::bytea), %s, %s, %s)""",
                (golden["workspace"]["id"], str(uuid4()), admission, status, body),
            )


@pytest.mark.parametrize(
    ("status", "body"),
    [(201, None), (None, Jsonb({})), (201, Jsonb(None)), (201, Jsonb([])), (500, Jsonb({}))],
)
def test_request_status_body_coherence_is_enforced(seeded_db, golden, status, body):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        operation = insert_operation(conn, wid)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO ledger.request_result
                   (workspace_id, client_id, route_family, key, operation_id, payload,
                    payload_sha256, http_status, response_body)
                   VALUES (%s, %s, 'purchase:v1', 'coherence', %s,
                           'x'::bytea, sha256('x'::bytea), %s, %s)""",
                (wid, str(uuid4()), operation, status, body),
            )


def test_business_and_scoped_request_identity_retention(seeded_db, golden):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        operation = insert_operation(conn, wid, admission="REJECTED")
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_operation(conn, wid, operation)
        client, key = insert_request(conn, wid, operation, key="retained", status=422)
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert_request(conn, wid, operation, client_id=client, key=key, status=422)
        insert_request(conn, wid, operation, key=key, status=422)  # Different client scope.
        insert_request(conn, wid, operation, client_id=client, key="new-key", status=422)
        assert conn.execute(
            "SELECT count(*) FROM ledger.request_result WHERE operation_id = %s",
            (operation,),
        ).fetchone() == (3,)
        for table in ("operation", "request_result"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"DELETE FROM ledger.{table} WHERE operation_id = %s", (operation,))


def test_pending_rows_cannot_pass_set_constraints_immediate(seeded_db, golden):
    wid = golden["workspace"]["id"]
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="operation is incomplete"):
            with conn.transaction():
                insert_operation(conn, wid, pending=True)
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
