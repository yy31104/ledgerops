"""Observe actual original/identity/balance waits before allowing reversal races."""

from uuid import UUID

import pytest
from psycopg import errors
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import submit_reversal
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import (
    assert_balances,
    assert_retained,
    reversal_input,
)

pytestmark = pytest.mark.integration
FIRST = "40000000-0000-4000-8000-000000000021"
SECOND = "40000000-0000-4000-8000-000000000022"


@pytest.mark.parametrize("identity", ["distinct-original-attempts", "same-key", "same-business"])
def test_original_serializes_reversals_and_identity_races_replay(seeded_db, golden, identity):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    first = reversal_input(golden, operation_id=FIRST, key="race-first")
    second = reversal_input(
        golden,
        operation_id=SECOND if identity == "distinct-original-attempts" else FIRST,
        key="race-first" if identity == "same-key" else "race-second",
    )
    workers = []
    with (
        seeded_db.connect("app") as blocker,
        seeded_db.connect("app") as writer_a,
        seeded_db.connect("app") as writer_b,
        seeded_db.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                # A must already own its request/business claims before waiting
                # here. B then waits at the specifically intended lock stage.
                blocker.execute(
                    "SELECT operation_id FROM ledger.operation "
                    "WHERE workspace_id=%s AND operation_id=%s FOR UPDATE",
                    (golden["workspace"]["id"], golden["purchase_request"]["operation_id"]),
                )
                workers.append(start_call(lambda: submit_reversal(writer_a, *first)))
                wait_for_blocker(
                    observer,
                    writer_a.info.backend_pid,
                    blocker.info.backend_pid,
                    workers[0],
                    query_fragment="FOR UPDATE",
                )
                workers.append(start_call(lambda: submit_reversal(writer_b, *second)))
                if identity == "distinct-original-attempts":
                    blockers = (blocker.info.backend_pid, writer_a.info.backend_pid)
                    statement = "FOR UPDATE"
                else:
                    blockers = writer_a.info.backend_pid
                    statement = (
                        "INSERT INTO ledger.request_result"
                        if identity == "same-key"
                        else "INSERT INTO ledger.operation"
                    )
                wait_for_blocker(
                    observer,
                    writer_b.info.backend_pid,
                    blockers,
                    workers[1],
                    query_fragment=statement,
                )
        finally:
            outcomes = [finish_call(worker) for worker in workers]
        assert writer_a.info.transaction_status == TransactionStatus.IDLE
        assert writer_b.info.transaction_status == TransactionStatus.IDLE
    assert [kind for kind, _ in outcomes] == ["returned", "returned"], outcomes
    first_result, second_result = (value for _, value in outcomes)
    assert first_result.http_status == 201 and not first_result.replayed
    if identity == "distinct-original-attempts":
        assert second_result.http_status == 422 and not second_result.replayed
        assert second_result.body["error"]["code"] == "ALREADY_REVERSED"
    else:
        assert second_result.http_status == 201 and second_result.replayed
        assert second_result.body == first_result.body
    for args, result in zip((first, second), (first_result, second_result), strict=True):
        assert_retained(seeded_db, args, result)
    assert_balances(seeded_db, golden, [100, 100, 0, 0])
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.purchase_reversal_effect").fetchone() == (
            1,
        )
        assert conn.execute(
            "SELECT count(*) FROM ledger.journal WHERE kind='REVERSAL'"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM ledger.reversal_attempt").fetchone() == (
            2 if identity == "distinct-original-attempts" else 1,
        )


def test_original_lock_precedes_balances_and_balances_follow_uuid_not_debit_role(seeded_db, golden):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    arguments = reversal_input(golden, operation_id=FIRST)
    customer = golden["purchase_request"]["customer_account_id"]
    merchant = golden["purchase_request"]["merchant_account_id"]
    assert customer < merchant  # Customer is credit side for reversal, but locks first.
    worker = None
    with (
        seeded_db.connect("app") as blocker,
        seeded_db.connect("app") as writer,
        seeded_db.connect("app") as probe,
        seeded_db.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT account_id FROM ledger.account_balance "
                    "WHERE workspace_id=%s AND account_id=%s FOR UPDATE",
                    (golden["workspace"]["id"], customer),
                )
                worker = start_call(lambda: submit_reversal(writer, *arguments))
                wait_for_blocker(
                    observer,
                    writer.info.backend_pid,
                    blocker.info.backend_pid,
                    worker,
                    query_fragment="FOR UPDATE OF b",
                )
                with probe.transaction():
                    # A debit-role-first implementation would already lock the
                    # merchant, causing this independent NOWAIT probe to fail.
                    assert probe.execute(
                        "SELECT account_id FROM ledger.account_balance "
                        "WHERE workspace_id=%s AND account_id=%s FOR UPDATE NOWAIT",
                        (golden["workspace"]["id"], merchant),
                    ).fetchone() == (UUID(merchant),)
                # The writer already owns the original while waiting on the
                # first balance; this establishes the original→balance order.
                with probe.transaction(), pytest.raises(errors.LockNotAvailable):
                    probe.execute(
                        "SELECT operation_id FROM ledger.operation "
                        "WHERE workspace_id=%s AND operation_id=%s FOR UPDATE NOWAIT",
                        (golden["workspace"]["id"], golden["purchase_request"]["operation_id"]),
                    )
        finally:
            if worker is not None:
                outcome = finish_call(worker)
    assert outcome[0] == "returned", outcome
    assert outcome[1].http_status == 201
    assert_balances(seeded_db, golden, [100, 100, 0, 0])
