"""Observe PostgreSQL wait edges across payout, purchase/reversal and maintenance."""

from contextlib import ExitStack
from uuid import uuid4

import pytest

from ledgerops.db.admission import (
    RetrySameIdentity,
    submit_payout,
    submit_purchase,
    submit_reversal,
)
from ledgerops.db.maintenance import freeze_workspace, resume_workspace
from ledgerops.db.payout_dispatch import claim_payout
from ledgerops.db.payout_resolution import receive_observation
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import NOW, fund_merchant, money, payout_input, payout_policy
from tests.integration.payout_observation_helpers import (
    event,
    observe,
    prepared_payout,
    resolution_state,
    trust,
)
from tests.integration.purchase_helpers import purchase_input
from tests.integration.reversal_helpers import reversal_input
from tests.integration.test_maintenance import ACTOR
from tests.integration.test_payout_resolution import check_final

pytestmark = pytest.mark.integration


def queued_merchant_writers(db, account_id, calls):
    """Queue real service calls behind one row, observing each dependency in turn."""
    workers = []
    with ExitStack() as stack:
        blocker = stack.enter_context(db.connect("owner"))
        observer = stack.enter_context(db.connect("admin"))
        connections = [stack.enter_context(db.connect(role)) for role, _ in calls]
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT account_id FROM ledger.account_balance WHERE account_id=%s FOR UPDATE",
                    (account_id,),
                )
                for conn, (_, callback) in zip(connections, calls, strict=True):
                    worker = start_call(lambda c=conn, fn=callback: fn(c))
                    workers.append(worker)
                    wait_for_blocker(
                        observer,
                        conn.info.backend_pid,
                        (blocker.info.backend_pid, connections[0].info.backend_pid),
                        worker,
                        query_fragment="account_balance",
                    )
            results = [finish_call(worker) for worker in workers]
            assert all(kind == "returned" for kind, _ in results), results
            return [result for _, result in results]
        finally:
            for conn, worker in zip(connections, workers, strict=False):
                if worker.thread.is_alive():
                    conn.cancel()
                    worker.thread.join(5)
                    assert not worker.thread.is_alive()


def assert_independent_fold(db, golden):
    with db.connect("reader") as conn:
        snapshot = capture_snapshot(conn, golden["workspace"]["id"])
    report = verify_snapshot(snapshot)
    assert report.healthy, report.as_dict()
    assert all(int(row["booked"]) >= int(row["reserved"]) for row in snapshot["balances"])
    return snapshot


@pytest.mark.parametrize("first", ["hold", "reversal"])
def test_hold_and_full_reversal_wait_for_same_merchant_and_only_first_can_spend(
    seeded_db, golden, first
):
    fund_merchant(seeded_db, golden)
    payout_args, reversal_args = payout_input(golden), reversal_input(golden)

    def hold(conn):
        return submit_payout(conn, *payout_args, policy=payout_policy(golden), clock=lambda: NOW)

    def reversal(conn):
        return submit_reversal(conn, *reversal_args)

    actions = {"hold": hold, "reversal": reversal}
    second = "reversal" if first == "hold" else "hold"
    results = queued_merchant_writers(
        seeded_db,
        str(payout_args[1].merchant_account_id),
        [("app", actions[first]), ("app", actions[second])],
    )
    assert [result.http_status for result in results] == (
        [202, 422] if first == "hold" else [201, 422]
    )
    assert results[1].body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert money(seeded_db, golden)[:3] == ((80, 62, 18) if first == "hold" else (0, 0, 0))
    snapshot = assert_independent_fold(seeded_db, golden)
    assert len(snapshot["reservations"]) == int(first == "hold")
    assert len(snapshot["reversal_effects"]) == int(first == "reversal")


def test_success_consumption_and_new_purchase_wait_on_shared_balance_without_lost_updates(
    seeded_db, golden
):
    command = prepared_payout(seeded_db, golden)
    success = event(seeded_db, command)
    purchase = purchase_input(
        golden, key="purchase-10-during-payout", operation_id=str(uuid4()), amount_minor="10"
    )

    def consume(conn):
        return receive_observation(
            conn,
            command.workspace_id,
            command.operation_id,
            success,
            trust=trust(command),
            clock=lambda: NOW,
        )

    settled, bought = queued_merchant_writers(
        seeded_db,
        str(command.merchant_account_id),
        [("worker", consume), ("app", lambda conn: submit_purchase(conn, *purchase))],
    )
    assert settled.status == "APPLIED" and bought.http_status == 201
    assert money(seeded_db, golden)[:3] == (28, 0, 28)
    snapshot = assert_independent_fold(seeded_db, golden)
    assert [row["booked"] for row in snapshot["balances"]] == ["38", "10", "28", "0"]
    assert [row["state"] for row in snapshot["reservation_events"]] == ["HELD", "CONSUMED"]
    assert len(snapshot["payout_resolutions"]) == 1


@pytest.mark.parametrize("path", ["claim", "observe"])
def test_freeze_waits_for_inflight_payout_transaction_and_rejects_new_work_until_resume(
    seeded_db, golden, path
):
    command = prepared_payout(seeded_db, golden)
    freeze_id = str(uuid4())
    accepted = event(seeded_db, command, "ACCEPTED", sequence=1)
    success = event(seeded_db, command, "SUCCEEDED", sequence=2)
    calls = []
    with (
        seeded_db.connect("owner") as blocker,
        seeded_db.connect("worker") as writer,
        seeded_db.connect("maintenance") as maintenance,
        seeded_db.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT operation_id FROM ledger.operation WHERE operation_id=%s FOR UPDATE",
                    (str(command.operation_id),),
                )
                call = start_call(
                    lambda: (
                        claim_payout(
                            writer, command.workspace_id, command.operation_id, clock=lambda: NOW
                        )
                        if path == "claim"
                        else receive_observation(
                            writer,
                            command.workspace_id,
                            command.operation_id,
                            accepted,
                            trust=trust(command),
                            clock=lambda: NOW,
                        )
                    )
                )
                calls.append((writer, call))
                wait_for_blocker(
                    observer,
                    writer.info.backend_pid,
                    blocker.info.backend_pid,
                    call,
                    query_fragment="ledger.operation",
                )
                freezing = start_call(
                    lambda: freeze_workspace(
                        maintenance, str(command.workspace_id), freeze_id, ACTOR, "drain payout"
                    )
                )
                calls.append((maintenance, freezing))
                wait_for_blocker(
                    observer,
                    maintenance.info.backend_pid,
                    writer.info.backend_pid,
                    freezing,
                    query_fragment="pg_advisory_xact_lock",
                )
                assert observer.execute("SELECT mode FROM ledger.workspace").fetchone() == (
                    "NORMAL",
                )
            results = [finish_call(call) for _, call in calls]
            assert all(kind == "returned" for kind, _ in results), results
            if path == "claim":
                assert results[0][1].action == "PUT"
            else:
                assert results[0][1].status == "OBSERVED"
            assert observer.execute("SELECT mode FROM ledger.workspace").fetchone() == ("FROZEN",)
            before = resolution_state(seeded_db)
            with pytest.raises(RetrySameIdentity):
                claim_payout(writer, command.workspace_id, command.operation_id, clock=lambda: NOW)
            with pytest.raises(RetrySameIdentity):
                receive_observation(
                    writer,
                    command.workspace_id,
                    command.operation_id,
                    success,
                    trust=trust(command),
                    clock=lambda: NOW,
                )
            assert resolution_state(seeded_db) == before
            assert money(seeded_db, golden)[:3] == (80, 62, 18)
            resume_workspace(
                maintenance, str(command.workspace_id), freeze_id, ACTOR, "hold projection checked"
            )
        finally:
            for conn, worker in calls:
                if worker.thread.is_alive():
                    conn.cancel()
                    worker.thread.join(5)
                    assert not worker.thread.is_alive()
    assert observe(seeded_db, command, success).status == "APPLIED"
    check_final(seeded_db, golden, command, "SUCCEEDED")
    assert_independent_fold(seeded_db, golden)
