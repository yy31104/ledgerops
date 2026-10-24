"""SQL-valid retained corruption must fail closed before replay or key linking."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from ledgerops.db.admission import InvariantViolation, submit_purchase
from tests.integration.purchase_helpers import invoke, purchase_input, retained_rows

pytestmark = pytest.mark.integration


def corrupt_retained_rows(db, command, changes):
    """Explicit owner-only defect injection; transition guards are restored at commit.

    The runtime role cannot perform this corruption. Keeping CHECKs, FKs and
    deferred terminal checks enabled proves these faults are beyond their scope.
    """
    triggers = {"operation": "operation_transition", "request_result": "request_transition"}
    with db.connect(role="owner") as conn, conn.transaction():
        for table, fields in changes.items():
            table_name, trigger_name = sql.Identifier(table), sql.Identifier(triggers[table])
            conn.execute(
                sql.SQL("ALTER TABLE ledger.{} DISABLE TRIGGER {}").format(table_name, trigger_name)
            )
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(field)) for field in fields
            )
            updated = conn.execute(
                sql.SQL(
                    "UPDATE ledger.{} SET {} WHERE workspace_id = %s AND operation_id = %s"
                ).format(table_name, assignments),
                (*fields.values(), str(command.workspace_id), str(command.operation_id)),
            )
            assert updated.rowcount == 1
            # ALTER TABLE cannot run with pending deferred trigger events.
            # Drain those checks without disabling the terminal/FK guards.
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            conn.execute(
                sql.SQL("ALTER TABLE ledger.{} ENABLE TRIGGER {}").format(table_name, trigger_name)
            )
        enabled = conn.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgname IN ('operation_transition', 'request_transition') ORDER BY tgname"
        ).fetchall()
        assert enabled == [("operation_transition", "O"), ("request_transition", "O")]


def assert_failed_replay_preserves_rows(db, arguments, *, new_key, message):
    principal, command, request_key = arguments
    if new_key:
        request_key = replace(request_key, key="candidate-link")
    before = retained_rows(db)
    with db.connect(role="app") as conn:
        with pytest.raises(InvariantViolation, match=message):
            submit_purchase(conn, principal, command, request_key)
        assert conn.info.transaction_status == TransactionStatus.IDLE
        if new_key:
            assert conn.execute(
                "SELECT count(*) FROM ledger.request_result "
                "WHERE workspace_id = %s AND client_id = %s AND route_family = %s AND key = %s",
                (
                    str(request_key.workspace_id),
                    str(request_key.client_id),
                    request_key.route_family,
                    request_key.key,
                ),
            ).fetchone() == (0,)
    assert retained_rows(db) == before


@pytest.mark.parametrize("new_key", [False, True], ids=["same-key", "new-key"])
def test_accepted_canonical_operation_without_journal_cannot_replay(seeded_db, golden, new_key):
    arguments = purchase_input(golden)
    _, command, key = arguments
    body = {
        "operation_id": str(command.operation_id),
        "admission": "accepted",
        "journal_id": "50000000-0000-4000-8000-000000000099",
        "amount_minor": "80",
        "currency": "PLN",
        "effect": "purchase_posted",
        "contract_version": 1,
    }
    # Direct terminal INSERT is permitted by SQL but cannot prove a business effect.
    with seeded_db.connect(role="app") as conn, conn.transaction():
        conn.execute(
            "INSERT INTO ledger.operation "
            "(workspace_id, operation_id, kind, payload, payload_sha256, "
            "admission, http_status, response_body) "
            "VALUES (%s, %s, 'PURCHASE', %s, %s, 'ACCEPTED', 201, %s)",
            (
                str(command.workspace_id),
                str(command.operation_id),
                command.canonical_bytes,
                command.payload_sha256,
                Jsonb(body),
            ),
        )
        conn.execute(
            "INSERT INTO ledger.request_result "
            "(workspace_id, client_id, route_family, key, operation_id, "
            "payload, payload_sha256, http_status, response_body) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 201, %s)",
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
    with seeded_db.connect(role="app") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ledger.journal WHERE workspace_id = %s AND operation_id = %s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT payload = %s, payload_sha256 = sha256(payload), response_body = %s "
            "FROM ledger.operation WHERE workspace_id = %s AND operation_id = %s",
            (
                command.canonical_bytes,
                Jsonb(body),
                str(command.workspace_id),
                str(command.operation_id),
            ),
        ).fetchone() == (True, True, True)
    assert_failed_replay_preserves_rows(
        seeded_db, arguments, new_key=new_key, message="Invalid retained purchase outcome"
    )


@pytest.mark.parametrize("field", ["payload", "http_status", "response_body"])
def test_request_operation_disagreement_is_not_replayed(seeded_db, golden, field):
    arguments = purchase_input(golden)
    first = invoke(seeded_db, *arguments)
    assert first.http_status == 201
    command = arguments[1]
    if field == "payload":
        # Keep the request identical to the incoming command so the intended
        # failure is retained disagreement, not an ordinary changed-key conflict.
        different = purchase_input(golden, amount_minor="81")[1]
        changes = {
            "operation": {
                "payload": different.canonical_bytes,
                "payload_sha256": different.payload_sha256,
            }
        }
    elif field == "http_status":
        changes = {"request_result": {"http_status": 422}}
    else:
        body = deepcopy(first.body)
        body["contract_version"] = True
        changes = {"request_result": {"response_body": Jsonb(body)}}
    corrupt_retained_rows(seeded_db, command, changes)
    with seeded_db.connect(role="app") as conn:
        operation = conn.execute(
            sql.SQL(
                "SELECT {} FROM ledger.operation WHERE workspace_id = %s AND operation_id = %s"
            ).format(sql.Identifier(field)),
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()[0]
        request = conn.execute(
            sql.SQL(
                "SELECT {} FROM ledger.request_result WHERE workspace_id = %s AND operation_id = %s"
            ).format(sql.Identifier(field)),
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()[0]
    if field == "response_body":
        assert type(operation["contract_version"]) is int
        assert request["contract_version"] is True
        assert json.dumps(operation, sort_keys=True) != json.dumps(request, sort_keys=True)
    else:
        assert operation != request
    assert_failed_replay_preserves_rows(
        seeded_db, arguments, new_key=False, message="Request and operation disagree"
    )


@pytest.mark.parametrize("new_key", [False, True], ids=["same-key", "new-key"])
@pytest.mark.parametrize("fault", ["unknown-prefix", "noncanonical-json"])
def test_invalid_canonical_payload_with_correct_digest_is_not_replayed(
    seeded_db, golden, new_key, fault
):
    arguments = purchase_input(golden)
    first = invoke(seeded_db, *arguments)
    assert first.http_status == 201
    command = arguments[1]
    if fault == "unknown-prefix":
        invalid = b"unknown-purchase:v99\n" + command.canonical_bytes.split(b"\n", 1)[1]
    else:
        prefix, encoded = command.canonical_bytes.split(b"\n", 1)
        invalid = prefix + b"\n " + encoded  # Valid JSON, invalid canonical bytes.
        assert json.loads(invalid.split(b"\n", 1)[1]) == json.loads(encoded)
    fields = {"payload": invalid, "payload_sha256": hashlib.sha256(invalid).digest()}
    corrupt_retained_rows(seeded_db, command, {"operation": fields, "request_result": fields})
    with seeded_db.connect(role="app") as conn:
        assert conn.execute(
            "SELECT payload, payload_sha256 = sha256(payload) FROM ledger.operation "
            "WHERE workspace_id = %s AND operation_id = %s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone() == (invalid, True)
    assert_failed_replay_preserves_rows(
        seeded_db, arguments, new_key=new_key, message="Invalid retained canonical purchase"
    )


@pytest.mark.parametrize("new_key", [False, True], ids=["same-key", "new-key"])
@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
def test_equal_python_values_with_wrong_json_types_are_not_valid_outcomes(
    seeded_db, golden, new_key, outcome
):
    arguments = purchase_input(golden, amount_minor="80" if outcome == "accepted" else "101")
    first = invoke(seeded_db, *arguments)
    assert first.http_status == (201 if outcome == "accepted" else 422)
    body = deepcopy(first.body)
    if outcome == "accepted":
        body["contract_version"] = True
    else:
        body["error"]["retryable"] = 0
    # Ordinary Python equality hides both defects; JSON types are contract data.
    assert body == first.body
    assert json.dumps(body, sort_keys=True) != json.dumps(first.body, sort_keys=True)
    command = arguments[1]
    corrupt_retained_rows(
        seeded_db,
        command,
        {
            "operation": {"response_body": Jsonb(body)},
            "request_result": {"response_body": Jsonb(body)},
        },
    )
    with seeded_db.connect(role="app") as conn:
        persisted = conn.execute(
            "SELECT response_body FROM ledger.operation "
            "WHERE workspace_id = %s AND operation_id = %s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()[0]
    if outcome == "accepted":
        assert persisted["contract_version"] is True
    else:
        assert type(persisted["error"]["retryable"]) is int
        assert persisted["error"]["retryable"] == 0
    assert_failed_replay_preserves_rows(
        seeded_db, arguments, new_key=new_key, message="Invalid retained purchase outcome"
    )
