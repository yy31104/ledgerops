"""Bounded thread coordination backed by observed PostgreSQL lock dependencies."""

from dataclasses import dataclass
from queue import Queue
from threading import Event, Thread
from time import monotonic

import pytest


@dataclass
class Worker:
    thread: Thread
    done: Event
    outcome: Queue


def start_call(call):
    done, outcome = Event(), Queue()

    def run():
        try:
            outcome.put(("returned", call()))
        except Exception as error:
            outcome.put(("raised", error))
        finally:
            done.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    return Worker(thread, done, outcome)


def finish_call(worker):
    worker.thread.join(timeout=12)
    assert not worker.thread.is_alive(), "database worker did not finish within watchdog"
    assert worker.done.is_set() and worker.outcome.qsize() == 1
    return worker.outcome.get_nowait()


def wait_for_blocker(observer, waiter_pid, blocker_pid, worker, *, query_fragment=None):
    """Require a lock relationship, optionally at a specific statement.

    blocker_pid may be a tuple when PostgreSQL can queue a row waiter behind
    either the holder or an earlier waiter. Both known transactions must remain
    open while this observation is made. Timeouts are test failures, not evidence.
    """
    expected = {blocker_pid} if isinstance(blocker_pid, int) else set(blocker_pid)
    deadline = monotonic() + 3
    while monotonic() < deadline:
        assert not worker.done.is_set(), "call completed before the required lock dependency"
        row = observer.execute(
            """SELECT wait_event_type, pg_blocking_pids(pid), query
               FROM pg_stat_activity WHERE pid = %s""",
            (waiter_pid,),
        ).fetchone()
        if row is not None and row[0] == "Lock" and expected.intersection(row[1]):
            assert query_fragment is None or query_fragment in row[2], (
                f"blocked at the wrong statement: {row[2]}"
            )
            return row
        worker.done.wait(timeout=0.01)  # poll a causal condition, not a schedule delay
    pytest.fail("required PostgreSQL lock dependency was not observed")
