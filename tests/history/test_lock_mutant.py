"""Remove the balance row lock and show the history checker catches the race.

Mutant: `FOR UPDATE OF b` is stripped from admission's balance read (test cursor only,
src/ledgerops unchanged). Both transactions pause after reading, so both decide on
the same balance version before either writes.

Two purchases of 30 from 100: the SQL deltas are relative (`credits=credits+%s`), so
the final balances are still correct and the independent fold agrees. Only the client
history shows that the second purchase checked funds against a stale version.

Two purchases of 80 from 100: the `booked >= reserved` CHECK aborts the loser, so the
committed history is clean, but the caller gets an integrity error instead of the
retained 422 refusal the real implementation commits.
"""

from threading import Barrier

import psycopg
import pytest

from ledgerops.db.admission import submit_purchase
from reference.journal_fold import assert_projection_matches
from tests.history.checker import check, dump_history, load_history
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.helpers import fetch_ledger
from tests.integration.purchase_helpers import purchase_input

pytestmark = pytest.mark.integration

_FIRST = "40000000-0000-4000-8000-0000000000a1"
_SECOND = "40000000-0000-4000-8000-0000000000a2"


def _inputs(golden, amount):
    return (
        purchase_input(golden, key="history-first", operation_id=_FIRST, amount_minor=str(amount)),
        purchase_input(
            golden, key="history-second", operation_id=_SECOND, amount_minor=str(amount)
        ),
    )


def _strip_lock_and_pause_after_both_reads(recorder):
    barrier = Barrier(2, timeout=5)

    def after_read(ops):
        if ops is not None and sum(op[0] == "r" for op in ops) == 2:
            barrier.wait()  # both transactions have read customer and merchant

    recorder.strip_lock = True
    recorder.after_read = after_read
    recorder.expect_anomalies = True


def _race(db, first, second):
    with db.connect(role="app") as a, db.connect(role="app") as b:
        workers = [
            start_call(lambda: submit_purchase(a, *first)),
            start_call(lambda: submit_purchase(b, *second)),
        ]
        return [finish_call(worker) for worker in workers]


def _balances(db):
    snapshot = fetch_ledger(db)
    assert_projection_matches(snapshot["accounts"], snapshot["entries"], snapshot["projections"])
    return {str(row["account_id"]): row["booked"] for row in snapshot["projections"]}


def _history_report(db, recorder, directory):
    """Same path as the fixture: write JSON Lines, then check the file offline."""
    with db.connect(role="admin") as admin:
        history = recorder.history(admin)
    path = directory / "mutant-history.jsonl"
    dump_history(history, path)
    return check(load_history(path)), history


def test_unlocked_purchases_keep_correct_totals_but_history_shows_lost_update(
    seeded_db, golden, history_recorder, tmp_path
):
    _strip_lock_and_pause_after_both_reads(history_recorder)
    first, second = _inputs(golden, 30)
    outcomes = _race(seeded_db, first, second)
    assert [kind for kind, _ in outcomes] == ["returned", "returned"]
    assert sorted(result.http_status for _, result in outcomes) == [201, 201]

    # The business oracle alone is satisfied: 100 - 30 - 30 = 40, fold matches projection.
    customer = golden["purchase_request"]["customer_account_id"]
    merchant = golden["purchase_request"]["merchant_account_id"]
    balances = _balances(seeded_db)
    assert (balances[customer], balances[merchant]) == (40, 60)

    report, history = _history_report(seeded_db, history_recorder, tmp_path)
    assert not report["valid"]
    assert {"lost-update", "non-adjacent-rmw", "G-single"} <= set(report["anomalies"])
    lost = {entry["key"] for entry in report["anomalies"]["lost-update"]}
    assert lost == {customer, merchant}
    # Both committed transactions read the same versions before writing.
    reads = [[op for op in txn["ops"] if op[0] == "r"] for txn in history]
    assert reads[0] == reads[1]


def test_unlocked_overdraft_is_stopped_by_check_but_loses_the_retained_refusal(
    seeded_db, golden, history_recorder, tmp_path
):
    _strip_lock_and_pause_after_both_reads(history_recorder)
    first, second = _inputs(golden, 80)
    outcomes = _race(seeded_db, first, second)
    kinds = sorted(
        "201" if kind == "returned" and value.http_status == 201 else type(value).__name__
        for kind, value in outcomes
    )
    # Real admission commits one 201 and one retained 422; the mutant raises instead.
    assert kinds == ["201", "CheckViolation"]
    assert any(isinstance(value, psycopg.errors.CheckViolation) for _, value in outcomes)

    report, history = _history_report(seeded_db, history_recorder, tmp_path)
    assert report["valid"], report["anomalies"]  # the aborted loser is not committed
    assert sorted(txn["status"] for txn in history) == ["fail", "ok"]


def test_locked_control_serializes_the_same_two_purchases(seeded_db, golden, history_recorder):
    first, second = _inputs(golden, 30)
    customer = golden["purchase_request"]["customer_account_id"]
    with (
        seeded_db.connect(role="app") as blocker,
        seeded_db.connect(role="app") as a,
        seeded_db.connect(role="app") as b,
        seeded_db.connect(role="admin") as observer,
    ):
        workers = []
        with blocker.transaction():
            blocker.execute(
                "SELECT 1 FROM ledger.account_balance WHERE workspace_id=%s AND account_id=%s "
                "FOR UPDATE",
                (golden["workspace"]["id"], customer),
            )
            workers.append(start_call(lambda: submit_purchase(a, *first)))
            wait_for_blocker(
                observer, a.info.backend_pid, blocker.info.backend_pid, workers[0],
                query_fragment="FOR UPDATE OF b",
            )  # fmt: skip
            workers.append(start_call(lambda: submit_purchase(b, *second)))
            wait_for_blocker(
                observer, b.info.backend_pid, (blocker.info.backend_pid, a.info.backend_pid),
                workers[1], query_fragment="FOR UPDATE OF b",
            )  # fmt: skip
        outcomes = [finish_call(worker) for worker in workers]
    assert sorted(result.http_status for _, result in outcomes) == [201, 201]
    # The fixture teardown checks this history and fails the test on any anomaly.
