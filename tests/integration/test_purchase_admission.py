"""Admission against real PostgreSQL: retained identity, ownership and money."""

from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
from uuid import UUID, uuid4

import pytest

from ledgerops.db.admission import BusinessIdConflict, IdempotencyConflict, NotAuthorized
from ledgerops.domain import ActorId, ClientId, WorkspaceId
from reference.journal_fold import assert_projection_matches, assert_purchase_matches
from tests.integration.helpers import fetch_ledger, post_fixture, seed_accounts
from tests.integration.purchase_helpers import (
    add_customer,
    fund_customer,
    invoke,
    purchase_input,
    retained_rows,
)

pytestmark = pytest.mark.integration


def _assert_no_change(db, before):
    assert retained_rows(db) == before


def _assert_rejection(result, code, operation_id):
    assert result.http_status == 422
    assert result.body.keys() == {"error"}
    error = result.body["error"]
    assert error.keys() == {"code", "message", "retryable", "operation_id", "request_id"}
    assert error["code"] == code
    assert error["operation_id"] == str(operation_id)
    assert error["retryable"] is False
    assert isinstance(error["message"], str) and error["message"]
    assert str(UUID(error["request_id"])) == error["request_id"]


def test_service_posts_frozen_purchase_and_retains_exact_bound_result(seeded_db, golden):
    before = fetch_ledger(seeded_db)
    principal, command, request_key = purchase_input(golden)
    result = invoke(seeded_db, principal, command, request_key)
    assert result.http_status == 201
    assert result.replayed is False
    journal_id = result.body["journal_id"]
    assert str(UUID(journal_id)) == journal_id
    assert result.body == {
        "operation_id": "40000000-0000-4000-8000-000000000002",
        "admission": "accepted",
        "journal_id": journal_id,
        "amount_minor": "80",
        "currency": "PLN",
        "effect": "purchase_posted",
        "contract_version": 1,
    }
    after = fetch_ledger(seeded_db)
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    assert_purchase_matches(
        golden["purchase_request"],
        after["accounts"],
        after["journals"],
        after["entries"],
        before["projections"],
        after["projections"],
    )
    balances = {row["account_id"]: row for row in after["projections"]}
    assert [balances[row["id"]]["booked"] for row in golden["accounts"]] == [100, 20, 80, 0]
    assert [balances[row["id"]]["version"] for row in golden["accounts"]] == [1, 2, 1, 0]
    with seeded_db.connect(role="app") as conn:
        operation = conn.execute(
            """SELECT admission, http_status, response_body, payload, payload_sha256
               FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s""",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()
        request = conn.execute(
            """SELECT operation_id, http_status, response_body, payload, payload_sha256
               FROM ledger.request_result WHERE workspace_id=%s AND client_id=%s
               AND route_family='purchase:v1' AND key=%s""",
            (str(request_key.workspace_id), str(request_key.client_id), request_key.key),
        ).fetchone()
    assert operation == (
        "ACCEPTED",
        201,
        result.body,
        command.canonical_bytes,
        command.payload_sha256,
    )
    assert request == (
        command.operation_id.value,
        201,
        result.body,
        command.canonical_bytes,
        command.payload_sha256,
    )


def test_same_key_replays_identical_result_without_mutation(seeded_db, golden):
    args = purchase_input(golden)
    first = invoke(seeded_db, *args)
    snapshot = retained_rows(seeded_db)
    replay = invoke(seeded_db, *args)
    assert (replay.http_status, replay.body, replay.replayed) == (201, first.body, True)
    _assert_no_change(seeded_db, snapshot)


@pytest.mark.parametrize("new_client", [False, True])
def test_new_key_or_client_same_operation_links_original_result(seeded_db, golden, new_client):
    first = invoke(seeded_db, *purchase_input(golden))
    before = retained_rows(seeded_db)
    options = {"key": "another-key"}
    if new_client:
        options["key"] = "purchase-80"
        options["client_id"] = "60000000-0000-4000-8000-000000000002"
    replay = invoke(seeded_db, *purchase_input(golden, **options))
    assert (replay.http_status, replay.body, replay.replayed) == (201, first.body, True)
    after = retained_rows(seeded_db)
    assert len(after.pop("request_result")) == 2
    before.pop("request_result")
    assert after == before


@pytest.mark.parametrize("changed", ["amount", "merchant", "operation"])
def test_same_key_changed_payload_conflicts_and_preserves_every_row(seeded_db, golden, changed):
    invoke(seeded_db, *purchase_input(golden))
    snapshot = retained_rows(seeded_db)
    changes = {
        "amount": {"amount_minor": "81"},
        "merchant": {"merchant_account_id": golden["accounts"][3]["id"]},
        "operation": {"operation_id": str(uuid4())},
    }[changed]
    with pytest.raises(IdempotencyConflict) as error:
        invoke(seeded_db, *purchase_input(golden, **changes))
    assert (error.value.http_status, error.value.code) == (409, "IDEMPOTENCY_CONFLICT")
    _assert_no_change(seeded_db, snapshot)


def test_new_key_changed_business_payload_rolls_back_provisional_claim(seeded_db, golden):
    first = invoke(seeded_db, *purchase_input(golden))
    snapshot = retained_rows(seeded_db)
    with pytest.raises(BusinessIdConflict) as error:
        invoke(seeded_db, *purchase_input(golden, key="candidate", amount_minor="81"))
    assert (error.value.http_status, error.value.code) == (409, "BUSINESS_ID_CONFLICT")
    _assert_no_change(seeded_db, snapshot)
    # The failed claim cannot poison this key; the correct original payload can link it.
    replay = invoke(seeded_db, *purchase_input(golden, key="candidate"))
    assert replay.body == first.body and replay.replayed is True


def test_insufficient_result_survives_funding_and_new_key_but_new_operation_can_succeed(
    seeded_db,
    golden,
):
    args = purchase_input(golden, amount_minor="101")
    before = retained_rows(seeded_db)
    rejected = invoke(seeded_db, *args)
    _assert_rejection(rejected, "INSUFFICIENT_AVAILABLE", args[1].operation_id)
    assert rejected.replayed is False
    rejected_rows = retained_rows(seeded_db)
    for table in ("journal", "journal_entry", "account_balance"):
        assert rejected_rows[table] == before[table]
    fund_customer(seeded_db, golden, 100)
    funded = retained_rows(seeded_db)
    replay = invoke(seeded_db, *args)
    assert (replay.http_status, replay.body, replay.replayed) == (422, rejected.body, True)
    _assert_no_change(seeded_db, funded)
    new_key = invoke(seeded_db, *purchase_input(golden, amount_minor="101", key="rejected-copy"))
    assert (new_key.http_status, new_key.body, new_key.replayed) == (422, rejected.body, True)
    accepted = invoke(
        seeded_db,
        *purchase_input(
            golden,
            amount_minor="101",
            key="new-decision",
            operation_id=str(uuid4()),
        ),
    )
    assert accepted.http_status == 201 and accepted.replayed is False
    with seeded_db.connect(role="app") as conn:
        assert (
            conn.execute(
                """SELECT booked FROM ledger.account_balance
               WHERE workspace_id=%s AND account_id=%s""",
                (golden["workspace"]["id"], golden["purchase_request"]["customer_account_id"]),
            ).fetchone()[0]
            == 99
        )
    final = fetch_ledger(seeded_db)
    assert_projection_matches(final["accounts"], final["entries"], final["projections"])


@pytest.mark.parametrize("target", ["customer", "merchant"])
def test_replay_ignores_later_disablement_but_new_purchase_is_not_authorized(
    seeded_db,
    golden,
    target,
):
    first = invoke(seeded_db, *purchase_input(golden))
    account = golden["purchase_request"][f"{target}_account_id"]
    with seeded_db.connect(role="owner") as conn:
        conn.execute(
            "UPDATE ledger.account SET enabled=false WHERE workspace_id=%s AND id=%s",
            (golden["workspace"]["id"], account),
        )
    replay = invoke(seeded_db, *purchase_input(golden))
    assert replay.body == first.body and replay.replayed is True
    linked = invoke(seeded_db, *purchase_input(golden, key="new-key"))
    assert linked.body == first.body and linked.replayed is True
    snapshot = retained_rows(seeded_db)
    with pytest.raises(NotAuthorized):
        invoke(seeded_db, *purchase_input(golden, key="new-op", operation_id=str(uuid4())))
    _assert_no_change(seeded_db, snapshot)


@pytest.mark.parametrize("collision", ["same-request", "same-operation"])
def test_collision_does_not_disclose_another_actors_retained_result(
    seeded_db,
    golden,
    collision,
):
    invoke(seeded_db, *purchase_input(golden))
    other_account, other_actor = add_customer(seeded_db, golden)
    snapshot = retained_rows(seeded_db)
    # The incoming customer is genuinely owned by this second actor. Authorization
    # of only the incoming command would wrongly expose a 409 or retained result.
    options = {"actor_id": other_actor, "customer_account_id": other_account}
    if collision == "same-operation":
        options["key"] = "second-actor-key"
    with pytest.raises(NotAuthorized) as error:
        invoke(seeded_db, *purchase_input(golden, **options))
    assert (error.value.http_status, error.value.code) == (404, "NOT_FOUND")
    assert str(golden["purchase_request"]["operation_id"]) not in str(error.value)
    _assert_no_change(seeded_db, snapshot)


@pytest.mark.parametrize("fault", ["missing-customer", "missing-merchant", "wrong-class", "actor"])
def test_invalid_or_unauthorized_accounts_leave_no_identity_claim(seeded_db, golden, fault):
    snapshot = retained_rows(seeded_db)
    changes = {
        "missing-customer": {"customer_account_id": str(uuid4())},
        "missing-merchant": {"merchant_account_id": str(uuid4())},
        "wrong-class": {"merchant_account_id": golden["accounts"][0]["id"]},
        "actor": {"actor_id": golden["actors"][3]["id"]},
    }[fault]
    with pytest.raises(NotAuthorized) as error:
        invoke(seeded_db, *purchase_input(golden, **changes))
    assert (error.value.http_status, error.value.code) == (404, "NOT_FOUND")
    _assert_no_change(seeded_db, snapshot)


@pytest.mark.parametrize("fault", ["principal-workspace", "key-workspace", "key-client"])
def test_principal_command_and_request_scope_must_match_before_claim(seeded_db, golden, fault):
    principal, command, request_key = purchase_input(golden)
    foreign_workspace = WorkspaceId.parse("10000000-0000-4000-8000-000000000099")
    if fault == "principal-workspace":
        principal = replace(principal, workspace_id=foreign_workspace)
    elif fault == "key-workspace":
        request_key = replace(request_key, workspace_id=foreign_workspace)
    else:
        request_key = replace(request_key, client_id=ClientId.parse(str(uuid4())))
    snapshot = retained_rows(seeded_db)
    with pytest.raises(NotAuthorized):
        invoke(seeded_db, principal, command, request_key)
    _assert_no_change(seeded_db, snapshot)


def test_same_uuid_accounts_operation_and_key_are_isolated_by_workspace(seeded_db, golden):
    other = deepcopy(golden)
    workspace = "10000000-0000-4000-8000-000000000002"
    other["workspace"]["id"] = workspace
    for collection in (
        "accounts",
        "operations",
        "journals",
        "entries",
        "before_purchase",
        "after_purchase",
    ):
        for row in other[collection]:
            row["workspace_id"] = workspace
    other["purchase_request"]["workspace_id"] = workspace
    other_actor = "20000000-0000-4000-8000-000000000099"
    other["accounts"][1]["owner_id"] = other_actor
    other["purchase_request"]["customer_id"] = other_actor
    seed_accounts(seeded_db, other)
    post_fixture(seeded_db, other, "OPENING")
    first = invoke(seeded_db, *purchase_input(golden))
    second = invoke(seeded_db, *purchase_input(other))
    assert first.http_status == second.http_status == 201
    assert first.replayed is second.replayed is False
    assert first.body["journal_id"] != second.body["journal_id"]
    with seeded_db.connect(role="app") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM ledger.journal").fetchone()[0] == 4
    snapshot = fetch_ledger(seeded_db)
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])
    # Same account UUID does not let the original actor read the other workspace's result.
    wrong_principal, wrong_command, wrong_key = purchase_input(
        other,
        actor_id=golden["purchase_request"]["customer_id"],
    )
    before = retained_rows(seeded_db)
    with pytest.raises(NotAuthorized):
        invoke(seeded_db, wrong_principal, wrong_command, wrong_key)
    _assert_no_change(seeded_db, before)


def test_second_valid_merchant_is_a_valid_explicit_recipient(seeded_db, golden):
    result = invoke(
        seeded_db,
        *purchase_input(
            golden,
            merchant_account_id=golden["accounts"][3]["id"],
        ),
    )
    assert result.http_status == 201
    snapshot = fetch_ledger(seeded_db)
    balances = {row["account_id"]: row["booked"] for row in snapshot["projections"]}
    assert [balances[row["id"]] for row in golden["accounts"]] == [100, 20, 0, 80]
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])


@pytest.mark.parametrize(
    ("amount", "additional_funding", "expected_customer"),
    [("1", 0, 99), ("100", 0, 0), ("9000000000000000", 8_999_999_999_999_900, 0)],
)
def test_valid_amount_boundaries_post_exact_minor_units(
    seeded_db,
    golden,
    amount,
    additional_funding,
    expected_customer,
):
    if additional_funding:
        fund_customer(seeded_db, golden, additional_funding)
    result = invoke(seeded_db, *purchase_input(golden, amount_minor=amount))
    assert result.http_status == 201
    assert result.body["amount_minor"] == amount
    snapshot = fetch_ledger(seeded_db)
    balances = {row["account_id"]: row["booked"] for row in snapshot["projections"]}
    assert balances[golden["purchase_request"]["customer_account_id"]] == expected_customer
    assert balances[golden["purchase_request"]["merchant_account_id"]] == int(amount)
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])


@pytest.mark.parametrize("field", ["credits", "version"])
def test_projection_limits_are_retained_rejections_without_financial_effect(
    seeded_db, golden, field
):
    # Deliberately extreme projection fixture, not a claimed valid journal fold.
    # It proves pre-write range checks rather than relying on SQL overflow faults.
    limit = 10**38 - 1 if field == "credits" else 2**63 - 1
    with seeded_db.connect(role="owner") as conn:
        conn.execute(
            f"UPDATE ledger.account_balance SET {field}=%s WHERE workspace_id=%s AND account_id=%s",
            (limit, golden["workspace"]["id"], golden["purchase_request"]["merchant_account_id"]),
        )
    before = retained_rows(seeded_db)
    args = purchase_input(golden)
    result = invoke(seeded_db, *args)
    _assert_rejection(result, "LIMIT_EXCEEDED", args[1].operation_id)
    after = retained_rows(seeded_db)
    for table in ("journal", "journal_entry", "account_balance"):
        assert after[table] == before[table]
    replay = invoke(seeded_db, *args)
    assert replay.body == result.body and replay.replayed is True
    _assert_no_change(seeded_db, after)


def test_large_totals_remain_exact_under_small_decimal_context(seeded_db, golden):
    # Privileged boundary setup only: these extreme projections do not claim a
    # complete matching journal history. Every parameter and expectation is int.
    maximum = 10**38 - 1
    workspace = golden["workspace"]["id"]
    customer = golden["purchase_request"]["customer_account_id"]
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect(role="owner") as conn, conn.transaction():
        conn.execute(
            """UPDATE ledger.account_balance SET debits=%s, credits=%s
               WHERE workspace_id=%s AND account_id=%s""",
            (maximum - 100, maximum, workspace, customer),
        )
        conn.execute(
            """UPDATE ledger.account_balance SET credits=%s
               WHERE workspace_id=%s AND account_id=%s""",
            (maximum - 80, workspace, merchant),
        )
    with localcontext() as context:
        context.prec = 6
        result = invoke(seeded_db, *purchase_input(golden))
        assert result.http_status == 201 and result.replayed is False
        with seeded_db.connect(role="app") as conn:
            customer_totals = conn.execute(
                """SELECT debits, credits, booked FROM ledger.account_balance
                   WHERE workspace_id=%s AND account_id=%s""",
                (workspace, customer),
            ).fetchone()
            merchant_totals = conn.execute(
                """SELECT debits, credits, booked FROM ledger.account_balance
                   WHERE workspace_id=%s AND account_id=%s""",
                (workspace, merchant),
            ).fetchone()
        assert customer_totals == (maximum - 20, maximum, 20)
        assert merchant_totals == (0, maximum, maximum)


def test_forged_actor_cannot_replay_known_success_even_with_its_exact_command(seeded_db, golden):
    principal, command, key = purchase_input(golden)
    invoke(seeded_db, principal, command, key)
    snapshot = retained_rows(seeded_db)
    forged = replace(principal, actor_id=ActorId.parse(golden["actors"][3]["id"]))
    with pytest.raises(NotAuthorized) as error:
        invoke(seeded_db, forged, command, key)
    assert (error.value.http_status, error.value.code) == (404, "NOT_FOUND")
    _assert_no_change(seeded_db, snapshot)


@pytest.mark.parametrize(("amount", "status", "booked"), [("70", 201, 30), ("71", 422, 100)])
def test_purchase_uses_available_balance_and_preserves_reservation(
    seeded_db, golden, amount, status, booked
):
    # A reservation is explicit test setup, not an implemented hold lifecycle.
    with seeded_db.connect(role="owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=30 WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], golden["purchase_request"]["customer_account_id"]),
        )
    result = invoke(seeded_db, *purchase_input(golden, amount_minor=amount))
    assert result.http_status == status
    if status == 422:
        assert result.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    with seeded_db.connect(role="app") as conn:
        assert conn.execute(
            "SELECT booked,reserved FROM ledger.account_balance "
            "WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], golden["purchase_request"]["customer_account_id"]),
        ).fetchone() == (booked, 30)
    snapshot = fetch_ledger(seeded_db)
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])
