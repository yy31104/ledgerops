"""Actual role privileges and commit-time payout lifecycle constraints."""

from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from tests.integration.payout_helpers import admit, fund_merchant, payout_input, payout_state

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "role,statement",
    [
        ("worker", "UPDATE ledger.account_balance SET normal_side=normal_side"),
        ("worker", "UPDATE ledger.operation SET payload=payload"),
        ("worker", "INSERT INTO ledger.reservation SELECT * FROM ledger.reservation"),
        ("worker", "UPDATE ledger.reservation SET gross_minor=gross_minor"),
        ("worker", "DELETE FROM ledger.transport_attempt"),
        ("app", "UPDATE ledger.payout SET knowledge='UNKNOWN',version=version+1"),
        ("app", "UPDATE ledger.dispatch_intent SET state='OBSERVING'"),
        ("reader", "UPDATE ledger.account_balance SET reserved=0"),
        ("maintenance", "UPDATE ledger.reservation SET state='HELD'"),
    ],
)
def test_runtime_roles_cannot_cross_financial_and_transport_boundaries(
    seeded_db, golden, role, statement
):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    before = payout_state(seeded_db)
    with seeded_db.connect(role) as conn:
        assert conn.execute("SELECT session_user,current_user").fetchone() == (
            f"ledger_{role}",
            f"ledger_{role}",
        )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)
    assert payout_state(seeded_db) == before


def test_accepted_payout_without_hold_and_intent_cannot_commit(seeded_db, golden):
    _, command, _ = payout_input(golden)
    with seeded_db.connect("app") as conn, pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO ledger.operation (workspace_id,operation_id,kind,payload,payload_sha256,"
            "admission,http_status,response_body) VALUES (%s,%s,'PAYOUT',%s,%s,'ACCEPTED',202,%s)",
            (
                str(command.workspace_id),
                str(command.operation_id),
                command.canonical_bytes,
                command.payload_sha256,
                Jsonb({"operation_id": str(command.operation_id)}),
            ),
        )
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE kind='PAYOUT'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger.payout SET dispatch_deadline=dispatch_deadline+interval '1 second'",
        "UPDATE ledger.payout SET provider_request=provider_request",
        "UPDATE ledger.reservation SET gross_minor=gross_minor",
        "DELETE FROM ledger.reservation_event",
        "INSERT INTO ledger.reservation_event SELECT workspace_id,payout_id,2,state,"
        "observation_id,recorded_at FROM ledger.reservation_event",
    ],
)
def test_owner_accidents_cannot_reopen_or_change_fixed_instructions(seeded_db, golden, statement):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    before = payout_state(seeded_db)
    with (
        seeded_db.connect("owner") as conn,
        pytest.raises((errors.CheckViolation, errors.InsufficientPrivilege)),
    ):
        conn.execute(statement)
    assert payout_state(seeded_db) == before


def test_second_hold_with_same_payout_is_rejected_by_primary_identity(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    before = payout_state(seeded_db)
    with seeded_db.connect("app") as conn, pytest.raises(errors.UniqueViolation):
        conn.execute("INSERT INTO ledger.reservation SELECT * FROM ledger.reservation")
    assert payout_state(seeded_db) == before


def test_accepted_purchase_cannot_use_payout_202_status(seeded_db, golden):
    with seeded_db.connect("app") as conn, pytest.raises(errors.CheckViolation):
        conn.execute(
            "INSERT INTO ledger.operation (workspace_id,operation_id,kind,payload,payload_sha256,"
            "admission,http_status,response_body) VALUES "
            "(%s,%s,'PURCHASE',%s,sha256(%s),'ACCEPTED',202,%s)",
            (
                golden["workspace"]["id"],
                str(uuid4()),
                b"fixture",
                b"fixture",
                Jsonb({"fixture": True}),
            ),
        )


def test_payout_migration_preserves_original_operation_payload_digest_check(seeded_db, golden):
    _, command, _ = payout_input(golden)
    with seeded_db.connect("app") as conn, pytest.raises(errors.CheckViolation) as caught:
        conn.execute(
            "INSERT INTO ledger.operation (workspace_id,operation_id,kind,payload,payload_sha256) "
            "VALUES (%s,%s,'PAYOUT',%s,%s)",
            (
                str(command.workspace_id),
                str(command.operation_id),
                command.canonical_bytes,
                bytes(32),
            ),
        )
    assert caught.value.diag.constraint_name == "operation_check"


def test_payout_migration_preserves_original_request_payload_digest_check(seeded_db, golden):
    principal, command, key = payout_input(golden)
    with seeded_db.connect("app") as conn, pytest.raises(errors.CheckViolation) as caught:
        conn.execute(
            "INSERT INTO ledger.request_result (workspace_id,client_id,route_family,key,"
            "operation_id,payload,payload_sha256) VALUES (%s,%s,'payout:v1',%s,%s,%s,%s)",
            (
                str(command.workspace_id),
                str(principal.client_id),
                key.key,
                str(command.operation_id),
                command.canonical_bytes,
                bytes(32),
            ),
        )
    assert caught.value.diag.constraint_name == "request_result_check"
