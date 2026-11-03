"""Real PostgreSQL: different merchants share one finite clearing commitment."""

from uuid import uuid4

import pytest

from ledgerops.db.admission import submit_payout
from ledgerops.db.payout_dispatch import claim_payout
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import NOW, admit, fund_merchant, payout_input, payout_policy
from tests.integration.payout_observation_helpers import event, observe
from tests.integration.purchase_helpers import fund_customer

pytestmark = pytest.mark.integration


def two_merchants(db, golden, *, clearing_capacity=100):
    fund_merchant(db, golden)
    second = golden["accounts"][3]
    fund_customer(db, golden, 80, customer_account_id=second["id"])
    with db.connect("owner") as conn:
        # Deliberately underfunded clearing projection, as in the review's
        # 100-vs-two62 example. This is not claimed to be a healthy journal fold.
        conn.execute(
            "UPDATE ledger.account_balance SET debits=%s,version=version+1 WHERE account_id=%s",
            (clearing_capacity, golden["accounts"][0]["id"]),
        )
    arguments = [
        payout_input(golden),
        payout_input(
            golden,
            key="other-merchant",
            operation_id=str(uuid4()),
            merchant_account_id=second["id"],
            actor_id=second["owner_id"],
        ),
    ]
    policies = [
        payout_policy(golden),
        payout_policy(
            golden,
            beneficiary_owners={
                arguments[1][1].beneficiary_id: arguments[1][0].actor_id,
            },
        ),
    ]
    return arguments, policies


def assert_one_hold(db):
    with db.connect("reader") as conn:
        assert conn.execute("SELECT gross_minor,state FROM ledger.reservation").fetchall() == [
            (62, "HELD")
        ]
        for table in ("payout", "reservation_event", "dispatch_intent"):
            assert conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (0,)


def test_second_merchant_rejected_before_hold_and_refusal_replays_after_funding(seeded_db, golden):
    args, policies = two_merchants(seeded_db, golden)
    assert admit(seeded_db, golden, args[0], policy=policies[0]).http_status == 202
    refused = admit(seeded_db, golden, args[1], policy=policies[1])
    assert refused.http_status == 422 and refused.body["error"]["code"] == "LIMIT_EXCEEDED"
    assert_one_hold(seeded_db)
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT admission,http_status FROM ledger.operation WHERE operation_id=%s",
            (args[1][1].operation_id.value,),
        ).fetchone() == ("REJECTED", 422)
        assert conn.execute(
            "SELECT response_body FROM ledger.request_result WHERE key='other-merchant'"
        ).fetchone() == (refused.body,)
    fund_customer(seeded_db, golden, 100)
    replay = admit(seeded_db, golden, args[1], policy=policies[1])
    assert replay.replayed and replay.body == refused.body
    assert_one_hold(seeded_db)


def test_different_merchants_serialize_on_clearing_then_read_committed_hold(seeded_db, golden):
    args, policies = two_merchants(seeded_db, golden)
    workers = []
    with seeded_db.connect("owner") as blocker, seeded_db.connect("admin") as observer:
        with seeded_db.connect("app") as first, seeded_db.connect("app") as second:
            connections = [first, second]
            try:
                with blocker.transaction():
                    blocker.execute(
                        "SELECT 1 FROM ledger.account_balance WHERE account_id=%s FOR UPDATE",
                        (golden["accounts"][0]["id"],),
                    )
                    for index, connection in enumerate(connections):
                        worker = start_call(
                            lambda i=index, c=connection: submit_payout(
                                c,
                                *args[i],
                                policy=policies[i],
                                clock=lambda: NOW,
                            )
                        )
                        workers.append(worker)
                        wait_for_blocker(
                            observer,
                            connection.info.backend_pid,
                            (blocker.info.backend_pid, first.info.backend_pid),
                            worker,
                            query_fragment="account_balance",
                        )
                results = [finish_call(worker) for worker in workers]
                assert all(kind == "returned" for kind, _ in results), results
                assert sorted(value.http_status for _, value in results) == [202, 422]
                assert_one_hold(seeded_db)
            finally:
                for connection, worker in zip(connections, workers, strict=False):
                    if worker.thread.is_alive():
                        connection.cancel()
                        worker.thread.join(5)


def test_unknown_execution_keeps_clearing_commitment(seeded_db, golden):
    args, policies = two_merchants(seeded_db, golden)
    assert admit(seeded_db, golden, args[0], policy=policies[0]).http_status == 202
    with seeded_db.connect("worker") as conn:
        assert (
            claim_payout(conn, args[0][1].workspace_id, args[0][1].operation_id, clock=lambda: NOW)
            is not None
        )
    result = admit(seeded_db, golden, args[1], policy=policies[1])
    assert result.http_status == 422
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT knowledge FROM ledger.payout").fetchone() == ("UNKNOWN",)
        assert conn.execute(
            "SELECT sum(gross_minor) FROM ledger.reservation WHERE state='HELD'"
        ).fetchone() == (62,)


@pytest.mark.parametrize("state", ["FAILED", "CLOSED_ABSENT"])
def test_negative_terminal_releases_commitment_for_new_business(seeded_db, golden, state):
    args, policies = two_merchants(seeded_db, golden)
    assert admit(seeded_db, golden, args[0], policy=policies[0]).http_status == 202
    command = args[0][1]
    assert observe(seeded_db, command, event(seeded_db, command, state)).status == "APPLIED"
    assert admit(seeded_db, golden, args[1], policy=policies[1]).http_status == 202
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT sum(gross_minor) FROM ledger.reservation WHERE state='HELD'"
        ).fetchone() == (62,)


def test_success_spends_capacity_without_counting_consumed_hold_twice(seeded_db, golden):
    args, policies = two_merchants(seeded_db, golden, clearing_capacity=180)
    assert admit(seeded_db, golden, args[0], policy=policies[0]).http_status == 202
    command = args[0][1]
    assert observe(seeded_db, command, event(seeded_db, command)).status == "APPLIED"
    assert admit(seeded_db, golden, args[1], policy=policies[1]).http_status == 202
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
            (golden["accounts"][0]["id"],),
        ).fetchone() == (118, 0)
        assert conn.execute(
            "SELECT sum(gross_minor) FROM ledger.reservation WHERE state='HELD'"
        ).fetchone() == (62,)
