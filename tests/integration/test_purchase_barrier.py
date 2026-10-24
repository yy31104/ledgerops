"""Service writes participate in the workspace barrier; maintenance is test-only SQL."""

from dataclasses import replace
from queue import Queue
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import pytest
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import Principal, RetrySameIdentity, submit_purchase
from ledgerops.domain.identities import ActorId, ClientId, OperationId, WorkspaceId
from ledgerops.domain.requests import PurchaseCommand, RequestKey

pytestmark = pytest.mark.integration

_BARRIER_KEY = "hashtextextended('ledgerops:workspace:' || %s::text, 0)"
_WATCHDOG_SECONDS = 3
_JOIN_SECONDS = 12


def _inputs(golden, *, key="barrier-purchase"):
    request = golden["purchase_request"]
    workspace = WorkspaceId.parse(request["workspace_id"])
    client = ClientId.parse("80000000-0000-4000-8000-000000000001")
    principal = Principal(
        workspace_id=workspace,
        client_id=client,
        actor_id=ActorId.parse(request["customer_id"]),
    )
    command = PurchaseCommand.from_payload(
        workspace,
        {
            "operation_id": request["operation_id"],
            "customer_account_id": request["customer_account_id"],
            "merchant_account_id": request["merchant_account_id"],
            "amount_minor": str(request["amount_minor"]),
            "currency": request["currency"],
        },
    )
    return principal, command, RequestKey(workspace, client, key)


def _snapshot(conn):
    """Include identity/result rows and projections when asserting no writes occurred."""
    tables = ("operation", "request_result", "journal", "journal_entry", "account_balance")
    return {
        table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY 1, 2").fetchall()
        for table in tables
    }


def _start_worker(call):
    finished = Event()
    outcome = Queue()

    def run():
        try:
            outcome.put(("returned", call()))
        except Exception as error:
            outcome.put(("raised", error))
        finally:
            finished.set()

    worker = Thread(target=run, daemon=True)
    worker.start()
    return worker, finished, outcome


def _finish(worker, finished, outcome):
    worker.join(timeout=_JOIN_SECONDS)
    assert not worker.is_alive(), "worker did not finish within the watchdog"
    assert finished.is_set()
    assert outcome.qsize() == 1
    return outcome.get_nowait()


def _wait_for_blocker(observer, waiter_pid, blocker_pid, finished):
    """Require an observed lock dependency; early errors/timeouts never count as success."""
    deadline = monotonic() + _WATCHDOG_SECONDS
    while monotonic() < deadline:
        assert not finished.is_set(), "operation finished before the required blocking relation"
        row = observer.execute(
            """SELECT wait_event_type, pg_blocking_pids(pid)
               FROM pg_stat_activity WHERE pid = %s""",
            (waiter_pid,),
        ).fetchone()
        if row is not None and row[0] == "Lock" and blocker_pid in row[1]:
            return
        finished.wait(timeout=0.01)  # bounded polling of the observed causal condition
    pytest.fail("required PostgreSQL lock dependency was not observed")


def _assert_barrier_released(observer, workspace):
    with observer.transaction():
        assert observer.execute(
            f"SELECT pg_try_advisory_xact_lock({_BARRIER_KEY})", (workspace,)
        ).fetchone() == (True,)


def _assert_mode_rejection(error, mode):
    assert isinstance(error, RetrySameIdentity), error
    # Unlike a lock timeout, this result must come from the mode read after the barrier.
    assert error.reason == "workspace_not_normal", f"{mode} produced {error!r}"
    assert error.http_status == 503
    assert error.code == "RETRY_SAME_IDENTITY"
    assert error.retryable is True


def test_normal_workspace_allows_purchase_and_releases_transaction_barrier(seeded_db, golden):
    principal, command, key = _inputs(golden)
    with seeded_db.connect() as conn, seeded_db.connect(role="admin") as observer:
        result = submit_purchase(conn, principal, command, key)
        assert result.http_status == 201
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert observer.execute(
            """SELECT booked FROM ledger.account_balance
               WHERE workspace_id = %s AND account_id = %s""",
            (str(command.workspace_id), str(command.customer_account_id)),
        ).fetchone() == (20,)
        _assert_barrier_released(observer, str(command.workspace_id))


@pytest.mark.parametrize("mode", ["FROZEN", "RESTORE"])
def test_nonnormal_workspace_rejects_without_claims_or_money_writes(seeded_db, golden, mode):
    principal, command, key = _inputs(golden)
    workspace = str(command.workspace_id)
    with seeded_db.connect(role="owner") as owner:
        owner.execute("UPDATE ledger.workspace SET mode = %s WHERE id = %s", (mode, workspace))
    with seeded_db.connect() as conn, seeded_db.connect(role="admin") as observer:
        before = _snapshot(observer)
        with pytest.raises(RetrySameIdentity) as failure:
            submit_purchase(conn, principal, command, key)
        _assert_mode_rejection(failure.value, mode)
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert _snapshot(observer) == before
        _assert_barrier_released(observer, workspace)


def test_exclusive_barrier_precedes_identity_claims_and_mode_read(seeded_db, golden):
    principal, command, key = _inputs(golden)
    workspace = str(command.workspace_id)
    with (
        seeded_db.connect(role="owner") as maintenance,
        seeded_db.connect() as writer,
        seeded_db.connect(role="admin") as observer,
    ):
        before = _snapshot(observer)
        worker = None
        try:
            with maintenance.transaction():
                maintenance.execute(f"SELECT pg_advisory_xact_lock({_BARRIER_KEY})", (workspace,))
                worker, finished, outcome = _start_worker(
                    lambda: submit_purchase(writer, principal, command, key)
                )
                _wait_for_blocker(
                    observer, writer.info.backend_pid, maintenance.info.backend_pid, finished
                )
                assert _snapshot(observer) == before
                # Uncommitted claims are invisible to SELECT. Relation write locks make
                # an incorrectly ordered INSERT/UPDATE observable before it commits.
                assert (
                    observer.execute(
                        """SELECT c.relname FROM pg_locks l
                       JOIN pg_class c ON c.oid = l.relation
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE l.pid = %s AND n.nspname = 'ledger'
                         AND l.mode IN ('RowExclusiveLock', 'ShareRowExclusiveLock',
                                        'ExclusiveLock', 'AccessExclusiveLock')""",
                        (writer.info.backend_pid,),
                    ).fetchall()
                    == []
                )
                maintenance.execute(
                    "UPDATE ledger.workspace SET mode = 'FROZEN' WHERE id = %s", (workspace,)
                )
        finally:
            if worker is not None:
                kind, value = _finish(worker, finished, outcome)
        assert kind == "raised", value
        _assert_mode_rejection(value, "FROZEN")
        assert _snapshot(observer) == before
        assert writer.info.transaction_status == TransactionStatus.IDLE
        _assert_barrier_released(observer, workspace)


def test_shared_barrier_drains_inflight_writer_before_freeze(seeded_db, golden):
    principal, command, key = _inputs(golden)
    workspace = str(command.workspace_id)
    with (
        seeded_db.connect(role="owner") as account_blocker,
        seeded_db.connect() as writer,
        seeded_db.connect(role="owner") as maintenance,
        seeded_db.connect(role="admin") as observer,
    ):
        writer_worker = freeze_worker = None

        def freeze_after_writers():
            with maintenance.transaction():
                maintenance.execute(f"SELECT pg_advisory_xact_lock({_BARRIER_KEY})", (workspace,))
                # A shared transaction lock is released only after the write commits:
                # these reads therefore have to see its final result, not a draft.
                stored = maintenance.execute(
                    """SELECT admission, http_status FROM ledger.operation
                       WHERE workspace_id = %s AND operation_id = %s""",
                    (workspace, str(command.operation_id)),
                ).fetchone()
                customer = maintenance.execute(
                    """SELECT booked FROM ledger.account_balance
                       WHERE workspace_id = %s AND account_id = %s""",
                    (workspace, str(command.customer_account_id)),
                ).fetchone()
                assert stored == ("ACCEPTED", 201)
                assert customer == (20,)
                maintenance.execute(
                    "UPDATE ledger.workspace SET mode = 'FROZEN' WHERE id = %s", (workspace,)
                )
            return "frozen after committed purchase"

        try:
            with account_blocker.transaction():
                account_blocker.execute(
                    """SELECT account_id FROM ledger.account_balance
                       WHERE workspace_id = %s AND account_id = %s FOR UPDATE""",
                    (workspace, str(command.customer_account_id)),
                )
                writer_worker, writer_finished, writer_outcome = _start_worker(
                    lambda: submit_purchase(writer, principal, command, key)
                )
                _wait_for_blocker(
                    observer,
                    writer.info.backend_pid,
                    account_blocker.info.backend_pid,
                    writer_finished,
                )
                assert observer.execute(
                    """SELECT count(*) FROM pg_locks WHERE pid = %s
                       AND locktype = 'advisory' AND mode = 'ShareLock' AND granted""",
                    (writer.info.backend_pid,),
                ).fetchone() == (1,)
                freeze_worker, freeze_finished, freeze_outcome = _start_worker(freeze_after_writers)
                _wait_for_blocker(
                    observer, maintenance.info.backend_pid, writer.info.backend_pid, freeze_finished
                )
                assert observer.execute(
                    "SELECT mode FROM ledger.workspace WHERE id = %s", (workspace,)
                ).fetchone() == ("NORMAL",)
            # Releasing the balance row lets the writer finish; only then may freeze finish.
        finally:
            if writer_worker is not None:
                writer_kind, writer_value = _finish(writer_worker, writer_finished, writer_outcome)
            if freeze_worker is not None:
                freeze_kind, freeze_value = _finish(freeze_worker, freeze_finished, freeze_outcome)
        assert writer_kind == "returned", writer_value
        assert writer_value.http_status == 201
        assert freeze_kind == "returned", freeze_value
        assert freeze_value == "frozen after committed purchase"
        assert writer.info.transaction_status == TransactionStatus.IDLE
        assert maintenance.info.transaction_status == TransactionStatus.IDLE
        _assert_barrier_released(observer, workspace)

        before = _snapshot(observer)
        next_command = replace(command, operation_id=OperationId(uuid4()))
        next_key = replace(key, key="after-freeze")
        with pytest.raises(RetrySameIdentity) as failure:
            submit_purchase(writer, principal, next_command, next_key)
        _assert_mode_rejection(failure.value, "FROZEN")
        assert _snapshot(observer) == before
