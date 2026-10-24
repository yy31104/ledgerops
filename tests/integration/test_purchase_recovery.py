"""Real process death and PostgreSQL-wire COMMIT ambiguity, followed by recovery."""

import multiprocessing
import signal
from dataclasses import replace
from threading import Event
from time import monotonic

import psycopg
import pytest

from ledgerops.db.admission import submit_purchase
from reference.journal_fold import assert_projection_matches, assert_purchase_matches
from tests.integration.helpers import fetch_ledger
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.purchase_helpers import invoke, purchase_input, retained_rows

pytestmark = pytest.mark.integration


def _post_then_await_business_reply(connection_settings, golden, control, reply, allow_reply):
    """A real caller process pauses AFTER service commit and BEFORE sending its reply."""
    try:
        with psycopg.connect(**connection_settings) as conn:
            result = submit_purchase(conn, *purchase_input(golden))
            control.send(("service_returned_after_commit", conn.info.backend_pid))
            if not allow_reply.wait(timeout=30):
                raise AssertionError("parent did not end the reply-loss scenario")
            reply.send((result.http_status, result.body))
    except Exception as exc:
        control.send(("child_failed", type(exc).__name__))
    finally:
        control.close()
        reply.close()


def _wait_backend_gone(db, backend_pid):
    """Observe backend termination, which also establishes release of transaction locks."""
    deadline = monotonic() + 5
    poll = Event()
    with db.connect(role="admin") as observer:
        while monotonic() < deadline:
            if observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (backend_pid,)
            ).fetchone() == (0,):
                return
            poll.wait(timeout=0.01)
    raise AssertionError("interrupted PostgreSQL backend did not exit")


def _stored_outcome(db, command):
    with db.connect(role="app") as conn:
        row = conn.execute(
            "SELECT http_status, response_body FROM ledger.operation "
            "WHERE workspace_id = %s AND operation_id = %s",
            (str(command.workspace_id), str(command.operation_id)),
        ).fetchone()
    assert row is not None and row[0] == 201
    return row


def _assert_one_purchase(db, golden, before):
    after = fetch_ledger(db)
    assert len(after["journals"]) == 2 and len(after["entries"]) == 4
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    assert_purchase_matches(
        golden["purchase_request"],
        after["accounts"],
        after["journals"],
        after["entries"],
        before["projections"],
        after["projections"],
    )
    with db.connect(role="app") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ledger.operation WHERE kind = 'PURCHASE'"
        ).fetchone() == (1,)


def _assert_same_and_new_key_recover_original(db, arguments, original):
    principal, command, key = arguments
    committed = retained_rows(db)
    replay = invoke(db, principal, command, key)
    assert replay.replayed is True
    assert (replay.http_status, replay.body) == original
    assert retained_rows(db) == committed
    new_key = replace(key, key="recover-original-operation")
    linked = invoke(db, principal, command, new_key)
    assert linked.replayed is True
    assert (linked.http_status, linked.body) == original
    after = retained_rows(db)
    assert len(after["request_result"]) == len(committed["request_result"]) + 1
    for table in ("operation", "journal", "journal_entry", "account_balance"):
        assert after[table] == committed[table]


def test_kill_after_service_commit_before_business_reply_recovers_original(seeded_db, golden):
    before = fetch_ledger(seeded_db)
    arguments = purchase_input(golden)
    context = multiprocessing.get_context("spawn")
    control_reader, control_writer = context.Pipe(duplex=False)
    reply_reader, reply_writer = context.Pipe(duplex=False)
    allow_reply = context.Event()
    child = context.Process(
        target=_post_then_await_business_reply,
        args=(
            _runtime_connection_settings(seeded_db, seeded_db.port),
            golden,
            control_writer,
            reply_writer,
            allow_reply,
        ),
    )
    try:
        child.start()
        control_writer.close()
        reply_writer.close()
        assert control_reader.poll(15), "child never reported its completed commit"
        marker, backend_pid = control_reader.recv()
        assert marker == "service_returned_after_commit", marker
        original = _stored_outcome(seeded_db, arguments[1])
        _assert_one_purchase(seeded_db, golden, before)
        # Control and business replies use distinct pipes. The event is never
        # released, so this proves the business caller received no success reply.
        assert not allow_reply.is_set()
        assert not reply_reader.poll(), "business reply escaped before the kill"
        child.kill()
        child.join(timeout=5)
        assert not child.is_alive() and child.exitcode == -signal.SIGKILL
        _wait_backend_gone(seeded_db, backend_pid)
        _assert_same_and_new_key_recover_original(seeded_db, arguments, original)
        _assert_one_purchase(seeded_db, golden, before)
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join(timeout=5)
            assert not child.is_alive(), "reply-loss child process did not stop"
            child.close()
        for endpoint in (control_reader, control_writer, reply_reader, reply_writer):
            endpoint.close()


def _runtime_connection_settings(db, port):
    # Only primitive values cross the spawn boundary; never print this mapping.
    return {
        "host": "127.0.0.1",
        "port": port,
        "dbname": db.name,
        "user": "ledger_app",
        "password": db.app_password,
        "sslmode": "disable",
        "gssencmode": "disable",
        "autocommit": True,
        "connect_timeout": 3,
        "options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000",
    }


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_unknown_commit_outcome_is_resolved_by_same_identity_not_assumed(seeded_db, golden, fault):
    arguments = purchase_input(golden)
    before = fetch_ledger(seeded_db)
    initial_rows = retained_rows(seeded_db)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        with psycopg.connect(**_runtime_connection_settings(seeded_db, proxy.port)) as conn:
            assert conn.execute("SELECT current_user").fetchone() == ("ledger_app",)
            backend_pid = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                submit_purchase(conn, *arguments)
            assert conn.closed
        assert proxy.finished.wait(timeout=5), "proxy never completed the observed fault"
        assert proxy.frontend_commit_seen.is_set(), "no actual frontend Q COMMIT frame"
        assert proxy.fault_applied.is_set(), "connection failed somewhere other than selected fault"
        assert proxy.commit_completions_forwarded == 0
        if fault == "before-forward":
            assert proxy.commit_queries_forwarded == 0
            assert not proxy.backend_commit_seen.is_set()
        else:
            assert proxy.commit_queries_forwarded == 1
            assert proxy.backend_commit_seen.is_set(), "no actual backend C COMMIT frame"
    _wait_backend_gone(seeded_db, backend_pid)

    if fault == "before-forward":
        # COMMIT left libpq, but not this proxy. Observe actual rollback before
        # recovery; client OperationalError alone cannot establish that outcome.
        assert retained_rows(seeded_db) == initial_rows
        recovered = invoke(seeded_db, *arguments)
        assert recovered.http_status == 201 and recovered.replayed is False
        original = (recovered.http_status, recovered.body)
    else:
        # PostgreSQL committed but its completion was withheld. Querying on a new
        # connection establishes the result that the original caller could not know.
        original = _stored_outcome(seeded_db, arguments[1])
        _assert_one_purchase(seeded_db, golden, before)
    _assert_same_and_new_key_recover_original(seeded_db, arguments, original)
    _assert_one_purchase(seeded_db, golden, before)
