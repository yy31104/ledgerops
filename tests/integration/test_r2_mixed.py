"""Observed cross-kind schedules, with literal outcomes fixed before execution."""

from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import RetrySameIdentity, submit_purchase, submit_reversal
from ledgerops.db.maintenance import freeze_workspace, resume_workspace
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import (
    assert_balances,
    original_rows,
    reversal_input,
    reversal_state,
)

pytestmark = pytest.mark.integration

# Initial opening100, purchase80 gives customer20/merchant80. Purchasing40
# before the refund must reject; refund-first permits it. These are literal
# outcomes, not calculated using admission or the independent verifier.
ORDER_EXPECTED = {
    "purchase-first": (422, [100, 100, 0, 0], 3),
    "reversal-first": (201, [100, 60, 40, 0], 4),
}
MIXED_BOOKED = [100, 90, 10, 0]  # purchase10 before refund80, plus original replay.
DIFFERENT_ORIGINALS_BOOKED = [100, 100, 0, 0]  # purchase50 twice, refund50 twice.


def _hold_customer(conn, golden):
    conn.execute(
        "SELECT account_id FROM ledger.account_balance "
        "WHERE workspace_id=%s AND account_id=%s FOR UPDATE",
        (golden["workspace"]["id"], golden["purchase_request"]["customer_account_id"]),
    )


def _assert_verified(db, golden, booked):
    # This helper compares literal totals and the independent reference fold;
    # the separately implemented exported-row verifier establishes B as well.
    assert_balances(db, golden, booked)
    with db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert report.reservation_supported


@pytest.mark.parametrize("order", ORDER_EXPECTED)
def test_purchase_and_reversal_follow_the_observed_balance_queue(seeded_db, golden, order):
    expected_status, expected_booked, expected_journals = ORDER_EXPECTED[order]
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    purchase = purchase_input(
        golden, operation_id=str(uuid4()), amount_minor="40", key="mixed-purchase40"
    )
    reversal = reversal_input(golden, key="mixed-refund80")
    workers = []
    with (
        seeded_db.connect("app") as blocker,
        seeded_db.connect("app") as first,
        seeded_db.connect("app") as second,
        seeded_db.connect("admin") as observer,
    ):
        calls = (
            ((submit_purchase, purchase), (submit_reversal, reversal))
            if order == "purchase-first"
            else ((submit_reversal, reversal), (submit_purchase, purchase))
        )
        try:
            with blocker.transaction():
                _hold_customer(blocker, golden)
                function, arguments = calls[0]
                workers.append(start_call(lambda: function(first, *arguments)))
                wait_for_blocker(
                    observer,
                    first.info.backend_pid,
                    blocker.info.backend_pid,
                    workers[0],
                    query_fragment="FOR UPDATE OF b",
                )
                function_two, arguments_two = calls[1]
                workers.append(start_call(lambda: function_two(second, *arguments_two)))
                wait_for_blocker(
                    observer,
                    second.info.backend_pid,
                    (blocker.info.backend_pid, first.info.backend_pid),
                    workers[1],
                    query_fragment="FOR UPDATE OF b",
                )
        finally:
            outcomes = [finish_call(worker) for worker in workers]
        assert all(
            conn.info.transaction_status == TransactionStatus.IDLE for conn in (first, second)
        )
    assert [kind for kind, _ in outcomes] == ["returned", "returned"], outcomes
    by_kind = {
        "purchase": outcomes[0 if order == "purchase-first" else 1][1],
        "reversal": outcomes[1 if order == "purchase-first" else 0][1],
    }
    assert by_kind["purchase"].http_status == expected_status
    assert by_kind["reversal"].http_status == 201
    assert not by_kind["purchase"].replayed and not by_kind["reversal"].replayed
    if expected_status == 422:
        assert by_kind["purchase"].body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    _assert_verified(seeded_db, golden, expected_booked)
    state = reversal_state(seeded_db)
    assert len(state["operation"]) == 4 and len(state["request_result"]) == 3
    assert len(state["journal"]) == expected_journals
    assert len(state["purchase_reversal_effect"]) == len(state["reversal_attempt"]) == 1
    assert original_rows(seeded_db, original_id) == original


def test_freeze_drains_purchase_reversal_and_original_replay_chain(seeded_db, golden):
    original_arguments = purchase_input(golden)
    original_result = invoke(seeded_db, *original_arguments)
    assert original_result.http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    purchase = purchase_input(
        golden, operation_id=str(uuid4()), amount_minor="10", key="mixed-purchase10"
    )
    reversal = reversal_input(golden, key="mixed-refund80")
    principal, command, key = original_arguments
    replay = (principal, command, replace(key, key="mixed-original-replay"))
    freeze, actor = str(uuid4()), str(uuid4())
    workspace = golden["workspace"]["id"]
    workers = []
    with (
        seeded_db.connect("app") as blocker,
        seeded_db.connect("app") as buyer,
        seeded_db.connect("app") as refunder,
        seeded_db.connect("app") as replayer,
        seeded_db.connect("maintenance") as maintenance,
        seeded_db.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                _hold_customer(blocker, golden)
                workers.append(start_call(lambda: submit_purchase(buyer, *purchase)))
                wait_for_blocker(
                    observer,
                    buyer.info.backend_pid,
                    blocker.info.backend_pid,
                    workers[0],
                    query_fragment="FOR UPDATE OF b",
                )
                workers.append(start_call(lambda: submit_reversal(refunder, *reversal)))
                wait_for_blocker(
                    observer,
                    refunder.info.backend_pid,
                    (blocker.info.backend_pid, buyer.info.backend_pid),
                    workers[1],
                    query_fragment="FOR UPDATE OF b",
                )
                workers.append(start_call(lambda: submit_purchase(replayer, *replay)))
                wait_for_blocker(
                    observer,
                    replayer.info.backend_pid,
                    refunder.info.backend_pid,
                    workers[2],
                    query_fragment="FOR UPDATE",
                )
                workers.append(
                    start_call(
                        lambda: freeze_workspace(
                            maintenance, workspace, freeze, actor, "mixed drain"
                        )
                    )
                )
                wait_for_blocker(
                    observer,
                    maintenance.info.backend_pid,
                    (buyer.info.backend_pid, refunder.info.backend_pid, replayer.info.backend_pid),
                    workers[3],
                    query_fragment="pg_advisory_xact_lock",
                )
                assert observer.execute(
                    "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
                ).fetchone() == ("NORMAL",)
                assert observer.execute("SELECT count(*) FROM ledger.operation").fetchone() == (2,)
        finally:
            outcomes = [finish_call(worker) for worker in workers]
        assert all(
            conn.info.transaction_status == TransactionStatus.IDLE
            for conn in (buyer, refunder, replayer, maintenance)
        )
    assert [kind for kind, _ in outcomes] == ["returned"] * 4, outcomes
    bought, reversed_result, replayed, frozen = (value for _, value in outcomes)
    assert bought.http_status == reversed_result.http_status == replayed.http_status == 201
    assert not bought.replayed and not reversed_result.replayed
    assert replayed.replayed and replayed.body == original_result.body
    assert frozen["freeze_id"] == freeze
    _assert_verified(seeded_db, golden, MIXED_BOOKED)
    state = reversal_state(seeded_db)
    assert len(state["operation"]) == len(state["request_result"]) == 4
    assert len(state["journal"]) == 4 and len(state["journal_entry"]) == 8
    assert len(state["purchase_reversal_effect"]) == 1
    before = reversal_state(seeded_db)
    fresh_purchase = purchase_input(
        golden, operation_id=str(uuid4()), amount_minor="1", key="after-mixed-freeze"
    )
    fresh_reversal = reversal_input(golden, key="after-mixed-freeze")
    for function, arguments in (
        (submit_purchase, fresh_purchase),
        (submit_reversal, fresh_reversal),
        (submit_purchase, replay),
    ):
        with seeded_db.connect("app") as conn, pytest.raises(RetrySameIdentity) as caught:
            function(conn, *arguments)
        assert caught.value.reason == "workspace_not_normal"
        assert reversal_state(seeded_db) == before
    with seeded_db.connect("maintenance") as conn:
        resumed = resume_workspace(conn, workspace, freeze, actor, "mixed history verified")
        assert resumed["resume_report"]["healthy"]
    assert original_rows(seeded_db, original_id) == original
    _assert_verified(seeded_db, golden, MIXED_BOOKED)


def test_distinct_original_reversals_share_balances_without_cross_original_waits(seeded_db, golden):
    first_id = golden["purchase_request"]["operation_id"]
    second_id = str(uuid4())
    for operation, key in ((first_id, "first50"), (second_id, "second50")):
        assert (
            invoke(
                seeded_db,
                *purchase_input(golden, operation_id=operation, amount_minor="50", key=key),
            ).http_status
            == 201
        )
    originals = {
        operation: original_rows(seeded_db, operation) for operation in (first_id, second_id)
    }
    arguments_a = reversal_input(golden, original_operation_id=first_id, key="refund-first50")
    arguments_b = reversal_input(golden, original_operation_id=second_id, key="refund-second50")
    workers = []
    with (
        seeded_db.connect("app") as blocker,
        seeded_db.connect("app") as first,
        seeded_db.connect("app") as second,
        seeded_db.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                _hold_customer(blocker, golden)
                workers.append(start_call(lambda: submit_reversal(first, *arguments_a)))
                wait_for_blocker(
                    observer,
                    first.info.backend_pid,
                    blocker.info.backend_pid,
                    workers[0],
                    query_fragment="FOR UPDATE OF b",
                )
                workers.append(start_call(lambda: submit_reversal(second, *arguments_b)))
                wait_for_blocker(
                    observer,
                    second.info.backend_pid,
                    (blocker.info.backend_pid, first.info.backend_pid),
                    workers[1],
                    query_fragment="FOR UPDATE OF b",
                )
        finally:
            outcomes = [finish_call(worker) for worker in workers]
        assert (
            first.info.transaction_status
            == second.info.transaction_status
            == TransactionStatus.IDLE
        )
    assert [kind for kind, _ in outcomes] == ["returned", "returned"], outcomes
    assert all(result.http_status == 201 and not result.replayed for _, result in outcomes)
    _assert_verified(seeded_db, golden, DIFFERENT_ORIGINALS_BOOKED)
    state = reversal_state(seeded_db)
    assert len(state["operation"]) == 5 and len(state["journal"]) == 5
    assert len(state["reversal_attempt"]) == len(state["purchase_reversal_effect"]) == 2
    assert {operation: original_rows(seeded_db, operation) for operation in originals} == originals
