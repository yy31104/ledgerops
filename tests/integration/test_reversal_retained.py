"""Stable ALREADY_REVERSED names an immutable success fact, not a balance decision."""

from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from ledgerops.db.admission import InvariantViolation, submit_reversal
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import reversal_input, reversal_state

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("new_key", [False, True], ids=["same-key", "new-key"])
def test_forged_already_reversed_without_success_cannot_replay_or_link_a_request(
    seeded_db, golden, new_key
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    principal, command, key = reversal_input(golden)
    body = {
        "error": {
            "code": "ALREADY_REVERSED",
            "message": "Purchase already reversed",
            "retryable": False,
            "operation_id": str(command.operation_id),
            "request_id": str(uuid4()),
        }
    }
    # Direct SQL satisfies terminal-state and relation structural constraints:
    # the extra semantic claim in its rejection code has no success fact behind it.
    with seeded_db.connect("app") as conn, conn.transaction():
        conn.execute(
            "INSERT INTO ledger.operation "
            "(workspace_id,operation_id,kind,payload,payload_sha256,admission,"
            "http_status,response_body) VALUES (%s,%s,'REVERSAL',%s,%s,'REJECTED',422,%s)",
            (
                str(command.workspace_id),
                str(command.operation_id),
                command.canonical_bytes,
                command.payload_sha256,
                Jsonb(body),
            ),
        )
        conn.execute(
            "INSERT INTO ledger.reversal_attempt "
            "(workspace_id,operation_id,original_operation_id) VALUES (%s,%s,%s)",
            (
                str(command.workspace_id),
                str(command.operation_id),
                str(command.original_operation_id),
            ),
        )
        conn.execute(
            "INSERT INTO ledger.request_result "
            "(workspace_id,client_id,route_family,key,operation_id,payload,"
            "payload_sha256,http_status,response_body) VALUES (%s,%s,%s,%s,%s,%s,%s,422,%s)",
            (
                str(key.workspace_id),
                str(key.client_id),
                key.route_family,
                key.key,
                str(command.operation_id),
                command.canonical_bytes,
                command.payload_sha256,
                Jsonb(body),
            ),
        )
    before = reversal_state(seeded_db)
    assert before["purchase_reversal_effect"] == []
    incoming_key = replace(key, key="candidate-replay-link") if new_key else key
    with seeded_db.connect("app") as conn:
        with pytest.raises(
            InvariantViolation, match="already-reversed result has no successful reversal"
        ):
            submit_reversal(conn, principal, command, incoming_key)
    assert reversal_state(seeded_db) == before
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
    assert report.structure_ok and report.projection_ok
    assert not report.request_intent_ok
    assert any(
        issue.code == "INVALID_REJECTION" and issue.field == "success_relation"
        for issue in report.issues
    )
