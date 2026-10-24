"""Retry actual PostgreSQL aborts, never mocked admission results.

The 40001 cases inject that SQLSTATE through a test-only database trigger; they
do not claim a natural SSI conflict under the service's READ COMMITTED isolation.
The deadlock case creates a real row/advisory-lock cycle between two backends.
"""

import json
import sys

import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from ledgerops.db import admission
from ledgerops.db.admission import RetrySameIdentity, submit_purchase
from reference.journal_fold import assert_projection_matches, assert_purchase_matches
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.helpers import fetch_ledger
from tests.integration.purchase_helpers import purchase_input, retained_rows

pytestmark = pytest.mark.integration

_NOTICE_PREFIX = "ledger_retry_probe:"
_DEADLOCK_KEY = 910230003


def _capture_attempts(conn):
    attempts = []

    def receive_notice(diagnostic):
        message = diagnostic.message_primary
        if message and message.startswith(_NOTICE_PREFIX):
            attempts.append(json.loads(message[len(_NOTICE_PREFIX) :]))

    conn.add_notice_handler(receive_notice)
    return attempts


def _observe_backoff(monkeypatch, conn, db, baseline):
    """Only spy on sleep: all aborts, transactions and persisted data are real PG."""
    observed = []
    original_sleep = admission.sleep

    def observe(delay):
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert not conn.closed
        assert retained_rows(db) == baseline
        error = sys.exception()
        observed.append((delay, getattr(error, "sqlstate", None)))
        original_sleep(delay)

    monkeypatch.setattr(admission, "sleep", observe)
    return observed


def _assert_backoffs(observed, expected_sqlstate, count):
    assert len(observed) == count
    for attempt_number, (delay, sqlstate) in enumerate(observed, 1):
        assert sqlstate == expected_sqlstate
        assert 0.01 * attempt_number <= delay <= 0.05 * attempt_number


def _install_terminal_fault(db, golden, *, failures, sqlstate, deferred=False):
    """A sequence survives rollback; notices preserve observations from aborted transactions."""
    assert failures in {1, 2, 3}
    assert sqlstate in {"40001", "23514"}
    request = golden["purchase_request"]
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute("CREATE SEQUENCE ledger.test_retry_attempts")
        conn.execute("GRANT USAGE, SELECT ON SEQUENCE ledger.test_retry_attempts TO ledger_app")
        conn.execute("""
            CREATE FUNCTION ledger.test_retry_probe() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
            DECLARE attempt bigint; journal uuid; probe json;
            BEGIN
              attempt := nextval('ledger.test_retry_attempts');
              SELECT journal_id INTO journal FROM ledger.journal
                WHERE workspace_id = NEW.workspace_id AND operation_id = NEW.operation_id;
              probe := json_build_object(
                'attempt', attempt, 'xid', pg_current_xact_id()::text,
                'operation_id', NEW.operation_id, 'journal_id', journal,
                'payload_hex', encode(NEW.payload, 'hex'),
                'request_keys', (SELECT json_agg(key) FROM ledger.request_result
                  WHERE workspace_id = NEW.workspace_id AND operation_id = NEW.operation_id),
                'operation_count', (SELECT count(*) FROM ledger.operation
                  WHERE workspace_id = NEW.workspace_id AND operation_id = NEW.operation_id),
                'journal_count', (SELECT count(*) FROM ledger.journal
                  WHERE workspace_id = NEW.workspace_id AND operation_id = NEW.operation_id),
                'entry_count', (SELECT count(*) FROM ledger.journal_entry
                  WHERE workspace_id = NEW.workspace_id AND journal_id = journal),
                'customer_booked', (SELECT booked FROM ledger.account_balance
                  WHERE workspace_id = NEW.workspace_id AND account_id = TG_ARGV[2]::uuid),
                'merchant_booked', (SELECT booked FROM ledger.account_balance
                  WHERE workspace_id = NEW.workspace_id AND account_id = TG_ARGV[3]::uuid)
              );
              RAISE NOTICE 'ledger_retry_probe:%', probe;
              IF attempt <= TG_ARGV[0]::bigint THEN
                RAISE EXCEPTION USING ERRCODE = TG_ARGV[1],
                  MESSAGE = 'test-only transaction retry fault',
                  CONSTRAINT = 'test_retry_probe';
              END IF;
              RETURN NEW;
            END $$
        """)
        conn.execute(
            sql.SQL("""
                CREATE {constraint}TRIGGER test_retry_probe {timing} UPDATE ON ledger.operation
                {defer}
                FOR EACH ROW WHEN (OLD.admission = 'IN_PROGRESS' AND NEW.admission = 'ACCEPTED')
                EXECUTE FUNCTION ledger.test_retry_probe(
                  {failures}, {state}, {customer}, {merchant})
            """).format(
                constraint=sql.SQL("CONSTRAINT " if deferred else ""),
                timing=sql.SQL("AFTER" if deferred else "BEFORE"),
                defer=sql.SQL("DEFERRABLE INITIALLY DEFERRED" if deferred else ""),
                failures=sql.Literal(str(failures)),
                state=sql.Literal(sqlstate),
                customer=sql.Literal(request["customer_account_id"]),
                merchant=sql.Literal(request["merchant_account_id"]),
            )
        )


def _remove_terminal_fault(db):
    with db.connect(role="owner") as conn:
        conn.execute("DROP TRIGGER test_retry_probe ON ledger.operation")


def _assert_fresh_complete_attempts(attempts, command, key, count):
    assert [attempt["attempt"] for attempt in attempts] == list(range(1, count + 1))
    assert len({attempt["xid"] for attempt in attempts}) == count
    assert len({attempt["journal_id"] for attempt in attempts}) == count
    for attempt in attempts:
        assert attempt["operation_id"] == str(command.operation_id)
        assert attempt["payload_hex"] == command.canonical_bytes.hex()
        assert attempt["request_keys"] == [key.key]
        assert attempt["operation_count"] == 1
        assert attempt["journal_count"] == 1
        assert attempt["entry_count"] == 2
        assert attempt["customer_booked"] == 20
        assert attempt["merchant_booked"] == 80


def _assert_single_effect(db, golden, before):
    after = fetch_ledger(db)
    assert len(after["journals"]) == 2
    assert len(after["entries"]) == 4
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    assert_purchase_matches(
        golden["purchase_request"],
        after["accounts"],
        after["journals"],
        after["entries"],
        before["projections"],
        after["projections"],
    )
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE kind = 'PURCHASE'"
        ).fetchone() == (1,)


@pytest.mark.parametrize("deferred", [False, True], ids=["during-write", "during-commit"])
def test_serialization_sqlstate_retries_two_whole_transactions_then_commits(
    seeded_db, golden, monkeypatch, deferred
):
    principal, command, key = purchase_input(golden)
    baseline, before = retained_rows(seeded_db), fetch_ledger(seeded_db)
    _install_terminal_fault(seeded_db, golden, failures=2, sqlstate="40001", deferred=deferred)
    with seeded_db.connect() as conn:
        attempts = _capture_attempts(conn)
        backoffs = _observe_backoff(monkeypatch, conn, seeded_db, baseline)
        result = submit_purchase(conn, principal, command, key)
        assert result.http_status == 201 and not result.replayed
        assert conn.info.transaction_status == TransactionStatus.IDLE
        _assert_backoffs(backoffs, "40001", 2)
        _assert_fresh_complete_attempts(attempts, command, key, 3)
        assert result.body["journal_id"] == attempts[-1]["journal_id"]
        assert conn.execute("SELECT last_value FROM ledger.test_retry_attempts").fetchone() == (3,)
    _assert_single_effect(seeded_db, golden, before)


def test_third_serialization_abort_exhausts_budget_without_retained_effects(
    seeded_db, golden, monkeypatch
):
    principal, command, key = purchase_input(golden)
    baseline, before = retained_rows(seeded_db), fetch_ledger(seeded_db)
    _install_terminal_fault(seeded_db, golden, failures=3, sqlstate="40001")
    with seeded_db.connect() as conn:
        attempts = _capture_attempts(conn)
        backoffs = _observe_backoff(monkeypatch, conn, seeded_db, baseline)
        with pytest.raises(RetrySameIdentity) as failure:
            submit_purchase(conn, principal, command, key)
        assert failure.value.reason == "transaction_retries_exhausted"
        assert failure.value.http_status == 503 and failure.value.retryable
        assert isinstance(failure.value.__cause__, errors.SerializationFailure)
        assert failure.value.__cause__.diag.constraint_name == "test_retry_probe"
        assert conn.info.transaction_status == TransactionStatus.IDLE
        _assert_backoffs(backoffs, "40001", 2)
        _assert_fresh_complete_attempts(attempts, command, key, 3)
        assert retained_rows(seeded_db) == baseline
        assert conn.execute("SELECT last_value FROM ledger.test_retry_attempts").fetchone() == (3,)
        _remove_terminal_fault(seeded_db)
        result = submit_purchase(conn, principal, command, key)
        assert result.http_status == 201 and not result.replayed
    _assert_single_effect(seeded_db, golden, before)


def test_integrity_sqlstate_is_never_internally_retried(seeded_db, golden, monkeypatch):
    principal, command, key = purchase_input(golden)
    baseline, before = retained_rows(seeded_db), fetch_ledger(seeded_db)
    _install_terminal_fault(seeded_db, golden, failures=1, sqlstate="23514")
    with seeded_db.connect() as conn:
        attempts = _capture_attempts(conn)
        backoffs = _observe_backoff(monkeypatch, conn, seeded_db, baseline)
        with pytest.raises(errors.CheckViolation) as failure:
            submit_purchase(conn, principal, command, key)
        assert failure.value.diag.constraint_name == "test_retry_probe"
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert backoffs == []
        _assert_fresh_complete_attempts(attempts, command, key, 1)
        assert retained_rows(seeded_db) == baseline
        assert conn.execute("SELECT last_value FROM ledger.test_retry_attempts").fetchone() == (1,)
        _remove_terminal_fault(seeded_db)
        assert submit_purchase(conn, principal, command, key).http_status == 201
    _assert_single_effect(seeded_db, golden, before)


def _install_deadlock_probe(db):
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute("CREATE SEQUENCE ledger.test_retry_attempts")
        conn.execute("GRANT USAGE, SELECT ON SEQUENCE ledger.test_retry_attempts TO ledger_app")
        conn.execute("""
            CREATE FUNCTION ledger.test_deadlock_probe() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
            DECLARE attempt bigint;
            BEGIN
              attempt := nextval('ledger.test_retry_attempts');
              RAISE NOTICE 'ledger_retry_probe:%', json_build_object(
                'attempt', attempt, 'xid', pg_current_xact_id()::text,
                'operation_id', NEW.operation_id, 'journal_id', NEW.journal_id);
              PERFORM pg_advisory_xact_lock(TG_ARGV[0]::bigint);
              RETURN NEW;
            END $$
        """)
        conn.execute(
            sql.SQL("""
                CREATE TRIGGER test_deadlock_probe AFTER INSERT ON ledger.journal
                FOR EACH ROW WHEN (NEW.kind = 'PURCHASE')
                EXECUTE FUNCTION ledger.test_deadlock_probe({})
            """).format(sql.Literal(str(_DEADLOCK_KEY)))
        )
    with db.connect(role="admin") as admin:
        database = admin.execute("SELECT current_database()").fetchone()[0]
        # Per-test-database setting: all service calls still use real ledger_app login.
        # The opposing backend's long timer makes the service the deadlock detector.
        admin.execute(
            sql.SQL("ALTER ROLE ledger_app IN DATABASE {} SET deadlock_timeout = '500ms'").format(
                sql.Identifier(database)
            )
        )


def test_real_deadlock_retries_after_rollback_and_commits_one_effect(
    seeded_db, golden, monkeypatch
):
    principal, command, key = purchase_input(golden)
    baseline, before = retained_rows(seeded_db), fetch_ledger(seeded_db)
    _install_deadlock_probe(seeded_db)
    with (
        seeded_db.connect() as service,
        seeded_db.connect(role="admin") as blocker,
        seeded_db.connect(role="admin") as observer,
    ):
        blocker.execute("SET deadlock_timeout = '10s'")
        blocker.execute("SET lock_timeout = '10s'")
        attempts = _capture_attempts(service)
        backoffs = _observe_backoff(monkeypatch, service, seeded_db, baseline)
        service_worker = blocker_worker = None
        try:
            with blocker.transaction():
                blocker.execute("SELECT pg_advisory_xact_lock(%s)", (_DEADLOCK_KEY,))
                service_worker = start_call(
                    lambda: submit_purchase(service, principal, command, key)
                )
                wait_for_blocker(
                    observer, service.info.backend_pid, blocker.info.backend_pid, service_worker
                )
                blocker_worker = start_call(
                    lambda: blocker.execute(
                        """SELECT account_id FROM ledger.account_balance
                       WHERE workspace_id = %s AND account_id = %s FOR UPDATE""",
                        (str(command.workspace_id), str(command.customer_account_id)),
                    ).fetchone()
                )
                wait_for_blocker(
                    observer, blocker.info.backend_pid, service.info.backend_pid, blocker_worker
                )
                blocker_kind, blocker_value = finish_call(blocker_worker)
                blocker_worker = None
                assert blocker_kind == "returned", blocker_value
                assert str(blocker_value[0]) == str(command.customer_account_id)
            # The real deadlock abort released service row locks; the opposing
            # transaction then acquired its row and released the advisory lock.
        finally:
            if blocker_worker is not None:
                blocker.cancel()
                finish_call(blocker_worker)
            if service_worker is not None:
                service_kind, service_value = finish_call(service_worker)
        assert service_kind == "returned", service_value
        assert service_value.http_status == 201 and not service_value.replayed
        assert service.info.transaction_status == TransactionStatus.IDLE
        _assert_backoffs(backoffs, "40P01", 1)
        assert [row["attempt"] for row in attempts] == [1, 2]
        assert len({row["xid"] for row in attempts}) == 2
        assert len({row["journal_id"] for row in attempts}) == 2
        assert {row["operation_id"] for row in attempts} == {str(command.operation_id)}
        assert service_value.body["journal_id"] == attempts[-1]["journal_id"]
    _assert_single_effect(seeded_db, golden, before)


@pytest.mark.parametrize("interruption", ["cancel", "lock-timeout"])
def test_interruption_does_not_retry_and_same_identity_recovers_after_unlock(
    seeded_db, golden, monkeypatch, interruption
):
    principal, command, key = purchase_input(golden)
    baseline, before = retained_rows(seeded_db), fetch_ledger(seeded_db)
    with (
        seeded_db.connect() as service,
        seeded_db.connect(role="owner") as blocker,
        seeded_db.connect(role="admin") as observer,
    ):
        backoffs = _observe_backoff(monkeypatch, service, seeded_db, baseline)
        worker = None
        try:
            with blocker.transaction():
                blocker.execute(
                    """SELECT account_id FROM ledger.account_balance
                       WHERE workspace_id = %s AND account_id = %s FOR UPDATE""",
                    (str(command.workspace_id), str(command.customer_account_id)),
                )
                worker = start_call(lambda: submit_purchase(service, principal, command, key))
                wait_for_blocker(
                    observer, service.info.backend_pid, blocker.info.backend_pid, worker
                )
                if interruption == "cancel":
                    assert observer.execute(
                        "SELECT pg_cancel_backend(%s)", (service.info.backend_pid,)
                    ).fetchone() == (True,)
                kind, value = finish_call(worker)
                worker = None
                assert kind == "raised" and isinstance(value, RetrySameIdentity), value
                assert value.reason == "database_contention"
                expected_error, expected_state = (
                    (errors.QueryCanceled, "57014")
                    if interruption == "cancel"
                    else (errors.LockNotAvailable, "55P03")
                )
                assert isinstance(value.__cause__, expected_error)
                assert value.__cause__.sqlstate == expected_state
                assert backoffs == []
                assert service.info.transaction_status == TransactionStatus.IDLE
                assert retained_rows(seeded_db) == baseline
                with observer.transaction():
                    assert observer.execute(
                        """SELECT pg_try_advisory_xact_lock(
                           hashtextextended('ledgerops:workspace:' || %s::text, 0))""",
                        (str(command.workspace_id),),
                    ).fetchone() == (True,)
        finally:
            if worker is not None:
                finish_call(worker)
        result = submit_purchase(service, principal, command, key)
        assert result.http_status == 201 and not result.replayed
        assert service.info.transaction_status == TransactionStatus.IDLE
    _assert_single_effect(seeded_db, golden, before)
