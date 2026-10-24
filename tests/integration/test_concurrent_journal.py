"""Real transactions cannot extend a journal across its immutable declaration."""

from queue import Queue
from threading import Barrier, Event, Thread
from time import monotonic
from uuid import UUID

import psycopg
import pytest

from tests.integration.helpers import write_journal

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 5
_JOIN_SECONDS = 12


def _timeouts(conn):
    conn.execute("SET lock_timeout = '5s'")
    conn.execute("SET statement_timeout = '8s'")
    conn.execute("SET idle_in_transaction_session_timeout = '15s'")


def _extra_accounts(db, workspace_id):
    """Privileged test-only setup; no production account/admission API exists."""
    accounts = [UUID(f"70000000-0000-4000-8000-{index:012d}") for index in range(1, 5)]
    with db.connect(role="owner") as conn, conn.transaction():
        _timeouts(conn)
        for account in accounts:
            conn.execute(
                """INSERT INTO ledger.account
                   (workspace_id, id, owner_id, class, normal_side, currency)
                   VALUES (%s, %s, %s, 'MERCHANT_LIABILITY', 'C', 'PLN')""",
                (workspace_id, account, account),
            )
    return accounts


def _append_pair(conn, workspace_id, journal_id, accounts, first_line):
    conn.execute(
        """INSERT INTO ledger.journal_entry
           (workspace_id, journal_id, line_no, account_id, side, amount_minor)
           VALUES (%s, %s, %s, %s, 'D', 5), (%s, %s, %s, %s, 'C', 5)""",
        (
            workspace_id,
            journal_id,
            first_line,
            accounts[0],
            workspace_id,
            journal_id,
            first_line + 1,
            accounts[1],
        ),
    )


def _history(conn, workspace_id, journal_id):
    return conn.execute(
        """SELECT line_no, account_id, side, amount_minor
           FROM ledger.journal_entry WHERE workspace_id = %s AND journal_id = %s
           ORDER BY line_no""",
        (workspace_id, journal_id),
    ).fetchall()


def _attempt_append(conn, workspace_id, journal_id, accounts, first_line, ready=None):
    try:
        with conn.transaction():
            _append_pair(conn, workspace_id, journal_id, accounts, first_line)
            if ready is not None:
                ready.wait(timeout=_WAIT_SECONDS)
    except psycopg.Error as error:
        return error.sqlstate, error.diag.message_primary
    return "COMMITTED", None


def _wait_for_fk_outcome_or_creator_lock(observer, appender_pid, creator_pid, finished):
    """Observe an actual outcome or lock; releasing A never depends on a sleep."""
    deadline = monotonic() + 3
    while monotonic() < deadline:
        if finished.is_set():
            return "finished"
        row = observer.execute(
            """SELECT wait_event_type, pg_blocking_pids(pid)
               FROM pg_stat_activity WHERE pid = %s""",
            (appender_pid,),
        ).fetchone()
        if row is not None and row[0] == "Lock" and creator_pid in row[1]:
            return "blocked_on_creator"
        # This is a bounded wait on the causal completion event, not a fixed
        # timing assumption about PostgreSQL or the thread scheduler.
        finished.wait(timeout=0.01)
    raise AssertionError("appender neither finished nor blocked on the uncommitted creator")


def _raise_worker_errors(errors):
    if not errors.empty():
        raise errors.get_nowait()


def test_append_racing_uncommitted_creation_cannot_commit(seeded_db, golden):
    db = seeded_db
    workspace_id = golden["workspace"]["id"]
    journal_id = golden["journals"][1]["journal_id"]
    accounts = _extra_accounts(db, workspace_id)
    attempt_started = Event()
    initial_finished = Event()
    creator_committed = Event()
    results = Queue()
    errors = Queue()

    with (
        db.connect(role="app") as creator,
        db.connect(role="app") as appender,
        db.connect(role="admin") as observer,
    ):
        for conn in (creator, appender, observer):
            _timeouts(conn)
        creator_pid = creator.info.backend_pid
        appender_pid = appender.info.backend_pid

        def append_during_and_after_creation():
            try:
                attempt_started.set()
                initial = _attempt_append(appender, workspace_id, journal_id, accounts[:2], 3)
                results.put(initial)
                initial_finished.set()
                if not creator_committed.wait(timeout=_WAIT_SECONDS):
                    raise AssertionError("creator did not publish its commit")
                # If the first INSERT saw no header, prove the same valid lines
                # are also refused by the seal once that header becomes visible.
                results.put(_attempt_append(appender, workspace_id, journal_id, accounts[:2], 3))
            except Exception as error:
                errors.put(error)
                initial_finished.set()

        worker = Thread(target=append_during_and_after_creation, daemon=True)
        try:
            with creator.transaction():
                write_journal(creator, golden, kind="PURCHASE")
                original = _history(creator, workspace_id, journal_id)
                assert len(original) == 2
                worker.start()
                assert attempt_started.wait(timeout=_WAIT_SECONDS), "appender never started"
                observation = _wait_for_fk_outcome_or_creator_lock(
                    observer, appender_pid, creator_pid, initial_finished
                )
                _raise_worker_errors(errors)
                if observation == "finished":
                    # Immediate FK invisibility is valid, but a timeout,
                    # uniqueness conflict, or successful commit is not.
                    first = results.get_nowait()
                    assert first[0] == "23503", first
                    results.put(first)
            creator_committed.set()
        finally:
            creator_committed.set()
            if worker.ident is not None:
                worker.join(timeout=_JOIN_SECONDS)
                assert not worker.is_alive(), "appender thread failed to finish"

        _raise_worker_errors(errors)
        assert results.qsize() == 2
        first, after_commit = results.get_nowait(), results.get_nowait()
        assert first[0] in {"23503", "23514"}, first
        assert after_commit == ("23514", "journal does not match immutable declaration")
        assert _history(observer, workspace_id, journal_id) == original


def test_two_concurrent_balanced_appends_both_fail_the_seal(seeded_db, golden):
    db = seeded_db
    workspace_id = golden["workspace"]["id"]
    journal_id = golden["journals"][0]["journal_id"]
    accounts = _extra_accounts(db, workspace_id)
    ready_to_commit = Barrier(3)
    results = Queue()
    errors = Queue()

    with db.connect(role="app") as observer:
        _timeouts(observer)
        original = _history(observer, workspace_id, journal_id)
    assert len(original) == 2

    def append(account_pair, first_line):
        try:
            with db.connect(role="app") as conn:
                _timeouts(conn)
                results.put(
                    _attempt_append(
                        conn, workspace_id, journal_id, account_pair, first_line, ready_to_commit
                    )
                )
        except Exception as error:
            errors.put(error)
            ready_to_commit.abort()

    # Distinct accounts AND line numbers prevent uniqueness failures from
    # masquerading as successful protection by the declaration check.
    workers = [
        Thread(target=append, args=(accounts[:2], 3), daemon=True),
        Thread(target=append, args=(accounts[2:], 5), daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        ready_to_commit.wait(timeout=_WAIT_SECONDS)
    finally:
        for worker in workers:
            worker.join(timeout=_JOIN_SECONDS)
        assert all(not worker.is_alive() for worker in workers), "appender threads did not finish"

    _raise_worker_errors(errors)
    assert results.qsize() == 2
    assert [results.get_nowait() for _ in workers] == [
        ("23514", "journal does not match immutable declaration"),
        ("23514", "journal does not match immutable declaration"),
    ]
    with db.connect(role="app") as observer:
        _timeouts(observer)
        assert _history(observer, workspace_id, journal_id) == original
