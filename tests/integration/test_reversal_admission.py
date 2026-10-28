"""Full reversal behavior through runtime credentials and independent frozen inputs."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import errors

from ledgerops.db.admission import (
    BusinessIdConflict,
    IdempotencyConflict,
    InvariantViolation,
    NotAuthorized,
    RetrySameIdentity,
)
from ledgerops.db.maintenance import freeze_workspace
from ledgerops.domain import ActorId, ClientId, OperationId, WorkspaceId
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.purchase_helpers import fund_customer, invoke, purchase_input
from tests.integration.reversal_helpers import (
    assert_balances,
    assert_retained,
    original_rows,
    reversal_input,
    reversal_state,
    reverse,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _purchase(db, golden):
    result = invoke(db, *purchase_input(golden))
    assert result.http_status == 201 and not result.replayed
    return result


def test_hand_frozen_rejected_funded_success_trace_and_unchanged_original(seeded_db, golden):
    corpus = json.loads((ROOT / "fixtures/golden/r2_commands.json").read_text())
    identifiers = {
        "P1": golden["purchase_request"]["operation_id"],
        "RA": "40000000-0000-4000-8000-000000000011",
        "RB": "40000000-0000-4000-8000-000000000012",
        "RC": "40000000-0000-4000-8000-000000000013",
    }
    labels = dict(zip(("CL", "C", "M1", "M2"), (a["id"] for a in golden["accounts"]), strict=True))
    original = None
    for index, step in enumerate(corpus["steps"]):
        command, expected = step["command"], step["expected"]
        result = None
        if index == 0:
            pass  # The seeded fixture supplies exactly the declared initial OPENING.
        elif command["kind"] == "OPENING":
            fund_customer(
                seeded_db,
                golden,
                command["amount_minor"],
                customer_account_id=labels[command["credit_account_id"]],
            )
        elif command["kind"] == "RESERVE":
            with seeded_db.connect("owner") as conn:
                conn.execute(
                    "UPDATE ledger.account_balance SET reserved=%s WHERE account_id=%s",
                    (command["reserved_minor"], labels[command["account_id"]]),
                )
        elif command["kind"] == "PURCHASE":
            result = invoke(seeded_db, *purchase_input(golden, key=command["key"]))
            if original is None:
                original = original_rows(seeded_db, identifiers["P1"])
        else:
            arguments = reversal_input(
                golden,
                operation_id=identifiers[command["operation_id"]],
                original_operation_id=identifiers[command["original_operation_id"]],
                key=command["key"],
                reason=command["reason"],
            )
            result = reverse(seeded_db, *arguments)
            assert_retained(seeded_db, arguments, result)
        if result is not None:
            code = result.body.get("effect", result.body.get("error", {}).get("code"))
            assert (result.http_status, code, result.replayed) == (
                expected["http_status"],
                expected["code"],
                expected["replayed"],
            )
        assert_balances(seeded_db, golden, [expected["booked"][label] for label in labels])
        with seeded_db.connect("app") as conn:
            assert conn.execute("SELECT count(*) FROM ledger.operation").fetchone() == (
                expected["operations"],
            )
            assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone() == (
                expected["requests"],
            )
            assert conn.execute("SELECT count(*) FROM ledger.journal").fetchone() == (
                expected["journals"],
            )
            assert conn.execute(
                "SELECT reserved FROM ledger.account_balance ORDER BY account_id"
            ).fetchall() == [(expected["reserved"][label],) for label in labels]
            effects = conn.execute(
                "SELECT original_operation_id,reversal_operation_id "
                "FROM ledger.purchase_reversal_effect"
            ).fetchall()
            assert {str(k): str(v) for k, v in effects} == {
                identifiers[k]: identifiers[v] for k, v in expected["reversal_effects"].items()
            }
        if original is not None:
            assert original_rows(seeded_db, identifiers["P1"]) == original
    with seeded_db.connect("reader") as conn:
        snapshot = capture_snapshot(conn, golden["workspace"]["id"])
        assert conn.execute("SELECT count(*) FROM ledger.reversal_attempt").fetchone() == (3,)
        for account, label in zip(golden["accounts"], labels, strict=True):
            totals = corpus["expected_final_totals"][label]
            assert conn.execute(
                "SELECT debits,credits,booked,reserved FROM ledger.account_balance "
                "WHERE account_id=%s",
                (account["id"],),
            ).fetchone() == tuple(
                totals[name] for name in ("debits", "credits", "booked", "reserved")
            )
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert not report.reservation_supported


@pytest.mark.parametrize("actor", ["customer", "other-merchant", "unknown"])
def test_only_original_merchant_can_submit_or_replay_without_disclosing_result(
    seeded_db, golden, actor
):
    _purchase(seeded_db, golden)
    original = reversal_input(golden)
    assert reverse(seeded_db, *original).http_status == 201
    owner = {
        "customer": golden["purchase_request"]["customer_id"],
        "other-merchant": golden["accounts"][3]["owner_id"],
        "unknown": str(uuid4()),
    }[actor]
    principal, command, key = original
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized) as caught:
        reverse(seeded_db, replace(principal, actor_id=ActorId.parse(owner)), command, key)
    assert caught.value.http_status == 404 and caught.value.code == "NOT_FOUND"
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("original_kind", ["missing", "opening", "rejected-purchase", "reversal"])
def test_ineligible_original_never_leaves_reversal_claims(seeded_db, golden, original_kind):
    if original_kind == "missing":
        original_id = str(uuid4())
    elif original_kind == "opening":
        original_id = golden["journals"][0]["operation_id"]
    elif original_kind == "rejected-purchase":
        purchase = purchase_input(golden, amount_minor="101")
        assert invoke(seeded_db, *purchase).http_status == 422
        original_id = str(purchase[1].operation_id)
    else:
        _purchase(seeded_db, golden)
        reversed_args = reversal_input(golden)
        assert reverse(seeded_db, *reversed_args).http_status == 201
        original_id = str(reversed_args[1].operation_id)
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized):
        reverse(seeded_db, *reversal_input(golden, original_operation_id=original_id))
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("identity", ["request-key", "business-id"])
def test_changed_reversal_reason_conflicts_without_creating_a_second_effect(
    seeded_db, golden, identity
):
    _purchase(seeded_db, golden)
    arguments = reversal_input(golden)
    first = reverse(seeded_db, *arguments)
    principal, command, key = arguments
    changed = replace(command, reason="changed refund reason")
    if identity == "business-id":
        key = replace(key, key="new-transport-key")
    before = reversal_state(seeded_db)
    expected = IdempotencyConflict if identity == "request-key" else BusinessIdConflict
    with pytest.raises(expected):
        reverse(seeded_db, principal, changed, key)
    assert reversal_state(seeded_db) == before
    assert reverse(seeded_db, *arguments).body == first.body


@pytest.mark.parametrize("disabled", ["customer_account_id", "merchant_account_id"])
def test_retained_reversal_ignores_later_account_disable_but_new_attempt_does_not(
    seeded_db, golden, disabled
):
    _purchase(seeded_db, golden)
    arguments = reversal_input(golden)
    first = reverse(seeded_db, *arguments)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account SET enabled=false WHERE id=%s",
            (golden["purchase_request"][disabled],),
        )
    replay = reverse(seeded_db, *arguments)
    assert replay.http_status == 201 and replay.body == first.body and replay.replayed
    principal, command, key = arguments
    with pytest.raises(NotAuthorized):
        reverse(
            seeded_db,
            principal,
            replace(command, operation_id=OperationId.parse(str(uuid4()))),
            replace(key, key="disabled-new-attempt"),
        )


def test_frozen_workspace_blocks_reversal_before_identity_claims(seeded_db, golden):
    _purchase(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, golden["workspace"]["id"], str(uuid4()), str(uuid4()), "freeze")
    before = reversal_state(seeded_db)
    with pytest.raises(RetrySameIdentity) as caught:
        reverse(seeded_db, *reversal_input(golden))
    assert caught.value.reason == "workspace_not_normal"
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("field", ["version", "credits"])
def test_reversal_cumulative_limits_are_retained_without_effect(seeded_db, golden, field):
    _purchase(seeded_db, golden)
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect("owner") as conn:
        if field == "version":
            conn.execute(
                "UPDATE ledger.account_balance SET version=%s WHERE account_id=%s",
                (2**63 - 1, merchant),
            )
        else:
            conn.execute(
                "UPDATE ledger.account_balance SET debits=%s,credits=%s WHERE account_id=%s",
                (10**38 - 21, 10**38 - 1, golden["purchase_request"]["customer_account_id"]),
            )
    before = reversal_state(seeded_db)
    args = reversal_input(golden)
    rejected = reverse(seeded_db, *args)
    assert rejected.http_status == 422 and rejected.body["error"]["code"] == "LIMIT_EXCEEDED"
    assert_retained(seeded_db, args, rejected)
    after = reversal_state(seeded_db)
    for table in ("journal", "journal_entry", "account_balance", "purchase_reversal_effect"):
        assert after[table] == before[table]
    assert reverse(seeded_db, *args).body == rejected.body


@pytest.mark.parametrize("role", ["maintenance", "reader"])
def test_nonbusiness_roles_cannot_create_reversal_relations(seeded_db, role):
    with seeded_db.connect(role) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO ledger.reversal_attempt SELECT * FROM ledger.reversal_attempt"
            )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO ledger.purchase_reversal_effect "
                "SELECT * FROM ledger.purchase_reversal_effect"
            )


@pytest.mark.parametrize("defect", ["canonical-payload", "terminal-body", "journal-owner"])
def test_corrupt_original_never_creates_reversal_claims(seeded_db, golden, defect):
    _purchase(seeded_db, golden)
    request = golden["purchase_request"]
    with seeded_db.connect("owner") as conn, conn.transaction():
        if defect == "journal-owner":
            conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
            conn.execute(
                "UPDATE ledger.journal_entry SET account_id=%s "
                "WHERE workspace_id=%s AND account_id=%s AND side='C'",
                (
                    golden["accounts"][3]["id"],
                    request["workspace_id"],
                    request["merchant_account_id"],
                ),
            )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")
        else:
            conn.execute("ALTER TABLE ledger.operation DISABLE TRIGGER operation_transition")
            if defect == "canonical-payload":
                conn.execute(
                    "UPDATE ledger.operation SET payload=%s,payload_sha256=sha256(%s) "
                    "WHERE workspace_id=%s AND operation_id=%s",
                    (
                        b"invalid original canonical request",
                        b"invalid original canonical request",
                        request["workspace_id"],
                        request["operation_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE ledger.operation SET response_body="
                    "jsonb_set(response_body,'{amount_minor}','\"79\"') "
                    "WHERE workspace_id=%s AND operation_id=%s",
                    (request["workspace_id"], request["operation_id"]),
                )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            conn.execute("ALTER TABLE ledger.operation ENABLE TRIGGER operation_transition")
    before = reversal_state(seeded_db)
    with pytest.raises(InvariantViolation):
        reverse(seeded_db, *reversal_input(golden))
    assert reversal_state(seeded_db) == before


def test_foreign_workspace_original_is_not_eligible_even_when_id_is_known(seeded_db, golden):
    other = deepcopy(golden)
    foreign_workspace = "10000000-0000-4000-8000-000000000009"
    other["workspace"]["id"] = foreign_workspace
    other["purchase_request"]["workspace_id"] = foreign_workspace
    for collection in (
        "accounts",
        "operations",
        "journals",
        "entries",
        "before_purchase",
        "after_purchase",
    ):
        for row in other[collection]:
            row["workspace_id"] = foreign_workspace
    seed_accounts(seeded_db, other)
    post_fixture(seeded_db, other, "OPENING")
    foreign = purchase_input(other, operation_id=str(uuid4()))
    assert invoke(seeded_db, *foreign).http_status == 201
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized):
        reverse(
            seeded_db,
            *reversal_input(golden, original_operation_id=str(foreign[1].operation_id)),
        )
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("scope", ["command-workspace", "key-workspace", "key-client"])
def test_reversal_scope_mismatch_is_rejected_before_any_claim(seeded_db, golden, scope):
    _purchase(seeded_db, golden)
    principal, command, key = reversal_input(golden)
    if scope == "command-workspace":
        command = replace(command, workspace_id=WorkspaceId.parse(str(uuid4())))
    elif scope == "key-workspace":
        key = replace(key, workspace_id=WorkspaceId.parse(str(uuid4())))
    else:
        key = replace(key, client_id=ClientId.parse(str(uuid4())))
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized):
        reverse(seeded_db, principal, command, key)
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("identity", ["request-key", "business-id"])
def test_owned_new_original_cannot_expose_another_merchants_retained_reversal(
    seeded_db, golden, identity
):
    _purchase(seeded_db, golden)
    first = reversal_input(golden)
    assert reverse(seeded_db, *first).http_status == 201
    own_purchase = purchase_input(
        golden,
        operation_id=str(uuid4()),
        key="purchase-to-other-merchant",
        merchant_account_id=golden["accounts"][3]["id"],
        amount_minor="20",
    )
    assert invoke(seeded_db, *own_purchase).http_status == 201
    incoming = reversal_input(
        golden,
        operation_id=str(first[1].operation_id),
        original_operation_id=str(own_purchase[1].operation_id),
        actor_id=golden["accounts"][3]["owner_id"],
        key=first[2].key if identity == "request-key" else "new-key-for-hidden-reversal",
    )
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized):
        reverse(seeded_db, *incoming)
    assert reversal_state(seeded_db) == before


def test_cross_kind_business_id_collision_cannot_expose_a_customer_purchase(seeded_db, golden):
    _purchase(seeded_db, golden)
    before = reversal_state(seeded_db)
    with pytest.raises(NotAuthorized):
        reverse(
            seeded_db,
            *reversal_input(golden, operation_id=golden["purchase_request"]["operation_id"]),
        )
    assert reversal_state(seeded_db) == before


@pytest.mark.parametrize("incoming", ["reversal", "purchase"])
def test_authorized_cross_kind_business_id_collision_is_named_conflict(seeded_db, golden, incoming):
    # Explicit privileged setup gives one actor both a customer and a merchant
    # account. This is permitted (owner/class is unique), so both histories may be
    # authorized; their distinct canonical kinds must still conflict by business ID.
    customer_actor = golden["purchase_request"]["customer_id"]
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("ALTER TABLE ledger.account DISABLE TRIGGER account_identity")
        conn.execute(
            "UPDATE ledger.account SET owner_id=%s WHERE id=%s",
            (customer_actor, golden["purchase_request"]["merchant_account_id"]),
        )
        conn.execute("ALTER TABLE ledger.account ENABLE TRIGGER account_identity")
    _purchase(seeded_db, golden)
    if incoming == "reversal":
        arguments = reversal_input(
            golden,
            operation_id=golden["purchase_request"]["operation_id"],
            actor_id=customer_actor,
        )
        call = reverse
    else:
        retained = reversal_input(golden, actor_id=customer_actor)
        assert reverse(seeded_db, *retained).http_status == 201
        arguments = purchase_input(
            golden, operation_id=str(retained[1].operation_id), key="cross-kind-purchase"
        )
        call = invoke
    before = reversal_state(seeded_db)
    with pytest.raises(BusinessIdConflict) as caught:
        call(seeded_db, *arguments)
    assert caught.value.http_status == 409 and caught.value.code == "BUSINESS_ID_CONFLICT"
    assert reversal_state(seeded_db) == before
