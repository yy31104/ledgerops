"""Frozen concurrent purchase histories executed through real runtime connections."""

from copy import deepcopy

import pytest
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import BusinessIdConflict, IdempotencyConflict, submit_purchase
from reference.journal_fold import assert_projection_matches, assert_purchase_matches
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.helpers import fetch_ledger, post_fixture, seed_accounts
from tests.integration.purchase_helpers import invoke, purchase_input, retained_rows

pytestmark = pytest.mark.integration

# Fixed before any worker runs. The debit race admits either winner, but never
# two successes or two rejections; no observed implementation output sets the IDs.
_FIRST_OPERATION = "40000000-0000-4000-8000-000000000002"
_SECOND_OPERATION = "40000000-0000-4000-8000-000000000003"
_FIRST_KEY = "concurrent-purchase-first"
_SECOND_KEY = "concurrent-purchase-second"


def _lock_balance(conn, golden, account_id):
    conn.execute(
        """SELECT account_id FROM ledger.account_balance
           WHERE workspace_id=%s AND account_id=%s FOR UPDATE""",
        (golden["workspace"]["id"], account_id),
    ).fetchone()


def _compete_behind_balance(db, golden, first, second, *, identity_table=None):
    """Hold A after its identity claims, then force B to hit the required wait."""
    workers = []
    with (
        db.connect(role="app") as blocker,
        db.connect(role="app") as writer_a,
        db.connect(role="app") as writer_b,
        db.connect(role="admin") as observer,
    ):
        try:
            with blocker.transaction():
                _lock_balance(blocker, golden, golden["purchase_request"]["customer_account_id"])
                workers.append(start_call(lambda: submit_purchase(writer_a, *first)))
                wait_for_blocker(
                    observer,
                    writer_a.info.backend_pid,
                    blocker.info.backend_pid,
                    workers[0],
                    query_fragment="FOR UPDATE OF b",
                )
                workers.append(start_call(lambda: submit_purchase(writer_b, *second)))
                if identity_table is None:
                    # Both separate business operations have reached the same
                    # balance lock. PostgreSQL may queue B on A's tuple wait.
                    blockers = (blocker.info.backend_pid, writer_a.info.backend_pid)
                    statement = "FOR UPDATE OF b"
                else:
                    blockers = writer_a.info.backend_pid
                    statement = f"INSERT INTO ledger.{identity_table}"
                wait_for_blocker(
                    observer,
                    writer_b.info.backend_pid,
                    blockers,
                    workers[1],
                    query_fragment=statement,
                )
                assert not workers[0].done.is_set() and not workers[1].done.is_set()
            # Only releasing the real row lock lets A commit and B observe it.
        finally:
            for worker in workers:
                worker.thread.join(timeout=12)
            outcomes = [finish_call(worker) for worker in workers]
        assert writer_a.info.transaction_status == TransactionStatus.IDLE
        assert writer_b.info.transaction_status == TransactionStatus.IDLE
    return outcomes


def _assert_one_financial_effect(db, golden, winner_operation, *, operations, requests):
    """Literal balance/ownership expectations plus an independent complete fold."""
    snapshot = fetch_ledger(db)
    assert len(snapshot["journals"]) == 2
    assert len(snapshot["entries"]) == 4
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])
    frozen_request = {**golden["purchase_request"], "operation_id": winner_operation}
    assert_purchase_matches(
        frozen_request,
        snapshot["accounts"],
        snapshot["journals"],
        snapshot["entries"],
        golden["before_purchase"],
        snapshot["projections"],
    )
    balances = {row["account_id"]: row for row in snapshot["projections"]}
    assert [balances[row["id"]]["booked"] for row in golden["accounts"]] == [100, 20, 80, 0]
    assert [balances[row["id"]]["version"] for row in golden["accounts"]] == [1, 2, 1, 0]
    with db.connect(role="app") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.operation").fetchone() == (operations,)
        assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone() == (requests,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE admission='IN_PROGRESS'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.request_result WHERE http_status IS NULL"
        ).fetchone() == (0,)


def _assert_persisted(db, inputs, result):
    principal, command, key = inputs
    with db.connect(role="app") as conn:
        operation = conn.execute(
            """SELECT admission,http_status,response_body FROM ledger.operation
               WHERE workspace_id=%s AND operation_id=%s""",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()
        request = conn.execute(
            """SELECT operation_id,http_status,response_body FROM ledger.request_result
               WHERE workspace_id=%s AND client_id=%s AND route_family=%s AND key=%s""",
            (str(command.workspace_id), str(principal.client_id), key.route_family, key.key),
        ).fetchone()
    admission = "ACCEPTED" if result.http_status == 201 else "REJECTED"
    assert operation == (admission, result.http_status, result.body)
    assert request == (command.operation_id.value, result.http_status, result.body)


def test_two_distinct_debits_of_80_from_100_commit_one_success_and_one_saved_rejection(
    seeded_db,
    golden,
):
    first = purchase_input(golden, key=_FIRST_KEY, operation_id=_FIRST_OPERATION)
    second = purchase_input(golden, key=_SECOND_KEY, operation_id=_SECOND_OPERATION)
    outcomes = _compete_behind_balance(seeded_db, golden, first, second)
    assert [kind for kind, _ in outcomes] == ["returned", "returned"], outcomes
    results = [value for _, value in outcomes]
    assert sorted(result.http_status for result in results) == [201, 422]
    assert all(result.replayed is False for result in results)
    winner = next(
        str(inputs[1].operation_id)
        for inputs, result in zip((first, second), results, strict=True)
        if result.http_status == 201
    )
    for inputs, result in zip((first, second), results, strict=True):
        _assert_persisted(seeded_db, inputs, result)
        if result.http_status == 422:
            assert result.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
            assert result.body["error"]["operation_id"] == str(inputs[1].operation_id)
            assert result.body["error"]["retryable"] is False
    _assert_one_financial_effect(seeded_db, golden, winner, operations=3, requests=2)
    retained = retained_rows(seeded_db)
    for inputs, original in zip((first, second), results, strict=True):
        replay = invoke(seeded_db, *inputs)
        assert (replay.http_status, replay.body, replay.replayed) == (
            original.http_status,
            original.body,
            True,
        )
    assert retained_rows(seeded_db) == retained


@pytest.mark.parametrize(
    ("identity_table", "same_payload", "second_key", "expected_exception", "request_count"),
    [
        ("request_result", True, _FIRST_KEY, None, 1),
        ("operation", True, _SECOND_KEY, None, 2),
        ("request_result", False, _FIRST_KEY, IdempotencyConflict, 1),
        ("operation", False, _SECOND_KEY, BusinessIdConflict, 1),
    ],
    ids=["same-request-replay", "same-operation-replay", "request-conflict", "operation-conflict"],
)
def test_identity_wait_observes_winner_then_replays_or_conflicts_without_duplicate_effect(
    seeded_db,
    golden,
    identity_table,
    same_payload,
    second_key,
    expected_exception,
    request_count,
):
    first = purchase_input(golden, key=_FIRST_KEY, operation_id=_FIRST_OPERATION)
    second = purchase_input(
        golden,
        key=second_key,
        operation_id=_FIRST_OPERATION,
        amount_minor="80" if same_payload else "81",
    )
    first_outcome, second_outcome = _compete_behind_balance(
        seeded_db,
        golden,
        first,
        second,
        identity_table=identity_table,
    )
    assert first_outcome[0] == "returned", first_outcome
    original = first_outcome[1]
    assert original.http_status == 201 and original.replayed is False
    _assert_persisted(seeded_db, first, original)
    if expected_exception is None:
        assert second_outcome[0] == "returned", second_outcome
        replay = second_outcome[1]
        assert (replay.http_status, replay.body, replay.replayed) == (201, original.body, True)
        _assert_persisted(seeded_db, second, replay)
    else:
        assert second_outcome[0] == "raised", second_outcome
        error = second_outcome[1]
        assert isinstance(error, expected_exception), error
        assert error.http_status == 409 and error.retryable is False
        assert error.code == (
            "IDEMPOTENCY_CONFLICT" if identity_table == "request_result" else "BUSINESS_ID_CONFLICT"
        )
    _assert_one_financial_effect(
        seeded_db,
        golden,
        _FIRST_OPERATION,
        operations=2,
        requests=request_count,
    )
    if expected_exception is BusinessIdConflict:
        # The losing transaction claimed a new transport key before waiting on
        # the business operation. Its entire claim must have rolled back.
        linked = invoke(
            seeded_db,
            *purchase_input(
                golden,
                key=_SECOND_KEY,
                operation_id=_FIRST_OPERATION,
            ),
        )
        assert linked.body == original.body and linked.replayed is True
        _assert_one_financial_effect(seeded_db, golden, _FIRST_OPERATION, operations=2, requests=2)


def test_lower_merchant_uuid_is_locked_before_higher_customer_uuid(db, golden):
    # Freeze an explicit ID-order variant before setup; do not edit the golden
    # fixture or infer expected account ownership from the resulting entries.
    variant = deepcopy(golden)
    original_customer = golden["purchase_request"]["customer_account_id"]
    higher_customer = "30000000-0000-4000-8000-000000000009"
    variant["accounts"][1]["id"] = higher_customer
    variant["purchase_request"]["customer_account_id"] = higher_customer
    for collection in ("entries", "before_purchase", "after_purchase"):
        for row in variant[collection]:
            if row["account_id"] == original_customer:
                row["account_id"] = higher_customer
    seed_accounts(db, variant)
    post_fixture(db, variant, "OPENING")
    inputs = purchase_input(variant, key="reverse-account-order")
    worker = None
    with (
        db.connect(role="app") as blocker,
        db.connect(role="app") as writer,
        db.connect(role="app") as probe,
        db.connect(role="admin") as observer,
    ):
        try:
            with blocker.transaction():
                _lock_balance(blocker, variant, variant["purchase_request"]["merchant_account_id"])
                worker = start_call(lambda: submit_purchase(writer, *inputs))
                wait_for_blocker(
                    observer,
                    writer.info.backend_pid,
                    blocker.info.backend_pid,
                    worker,
                    query_fragment="FOR UPDATE OF b",
                )
                # If the handler locked customer first by business role rather
                # than UUID, this NOWAIT probe would fail while merchant is blocked.
                with probe.transaction():
                    assert probe.execute(
                        """SELECT account_id FROM ledger.account_balance
                           WHERE workspace_id=%s AND account_id=%s FOR UPDATE NOWAIT""",
                        (variant["workspace"]["id"], higher_customer),
                    ).fetchone() == (inputs[1].customer_account_id.value,)
        finally:
            if worker is not None:
                outcome = finish_call(worker)
        assert outcome[0] == "returned", outcome
        assert outcome[1].http_status == 201
        assert writer.info.transaction_status == TransactionStatus.IDLE
    _assert_one_financial_effect(db, variant, _FIRST_OPERATION, operations=2, requests=1)
