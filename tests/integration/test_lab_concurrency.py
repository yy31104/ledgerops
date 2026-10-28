"""HTTP single steps must expose real PostgreSQL waits, outcomes and cleanup."""

import pytest

from lab import concurrency
from tests.integration.test_lab import call
from tests.integration.test_lab import http_lab as http_lab

pytestmark = pytest.mark.integration


def _action(server, action, **arguments):
    status, state = call(
        server.origin,
        "/api/action",
        {"action": action, **arguments},
        server.token,
    )
    assert status == 200, state
    return state


def _owned_pids(demo):
    pids = {actor["pid"] for actor in demo["actors"] if actor.get("pid")}
    if demo.get("observer_pid"):
        pids.add(demo["observer_pid"])
    return pids


def _actual_waits(server, demo):
    """An independent connection verifies the page's measurements, not its labels."""
    pids = _owned_pids(demo)
    assert pids
    observed = {row["pid"]: row for row in demo["observation"]["rows"]}
    with server.session.db.connect("admin") as observer:
        actual = observer.execute(
            "SELECT pid,state,wait_event_type,query,pg_blocking_pids(pid) "
            "FROM pg_stat_activity WHERE pid=ANY(%s)",
            (list(pids),),
        ).fetchall()
    waits = []
    for pid, state, wait_type, query, blockers in actual:
        if wait_type != "Lock":
            continue
        assert pid in observed
        displayed = observed[pid]
        assert displayed["state"] == state
        assert displayed["wait_event_type"] == "Lock"
        assert displayed["query"] == query
        assert set(displayed["blocking_pids"]) == set(blockers)
        assert blockers and set(blockers) <= pids
        waits.append(displayed)
    assert waits, "no actual PostgreSQL lock wait was observed"
    return waits


def _assert_final_balances(state):
    balances = {row["label"]: row["booked"] for row in state["balances"]}
    assert balances == {"clearing": "100", "customer": "20", "M1": "80", "M2": "0"}
    report = state["concurrency"]["final"]["report"]
    assert report["structure_ok"] and report["projection_ok"] and report["request_intent_ok"]
    assert report["healthy"] and not report["issues"]


def _assert_owned_connections_gone(server, pids):
    with server.session.db.connect("admin") as observer:
        assert (
            observer.execute(
                "SELECT pid FROM pg_stat_activity WHERE pid=ANY(%s)", (list(pids),)
            ).fetchall()
            == []
        )


def test_http_two_debits_advance_only_after_observed_wait_and_finish_one_success(http_lab):
    state = _action(http_lab, "concurrency_start", scenario="purchase_race")
    demo = state["concurrency"]
    assert demo["kind"] == "purchase_race" and demo["stage"] == 1
    waits = _actual_waits(http_lab, demo)
    assert len(waits) == 1 and "FOR UPDATE OF b" in waits[0]["query"]
    assert not demo["complete"]
    first = next(actor for actor in demo["actors"] if actor["name"] == "purchase_a")
    commands = [entry["sql"] for entry in first["trace"]]
    assert "SELECT set_config('lock_timeout', '5s', true)" in commands
    assert "SELECT set_config('lock_timeout', '10min', true)" in commands
    assert "SELECT set_config('statement_timeout', '10min', true)" in commands

    state = _action(http_lab, "concurrency_advance")
    demo = state["concurrency"]
    assert demo["stage"] == 2
    waits = _actual_waits(http_lab, demo)
    assert len(waits) == 2 and all("FOR UPDATE OF b" in row["query"] for row in waits)
    waiting_pids = {row["pid"] for row in waits}
    assert any(set(row["blocking_pids"]) & waiting_pids for row in waits)
    assert {row["label"]: row["booked"] for row in state["balances"]}["customer"] == "100"

    state = _action(http_lab, "concurrency_advance")
    demo = state["concurrency"]
    assert demo["stage"] == 3 and demo["complete"] and demo["status"] == "completed"
    statuses = sorted(actor["http_status"] for actor in demo["actors"] if actor.get("http_status"))
    assert statuses == [201, 422]
    refusal = next(actor for actor in demo["actors"] if actor.get("http_status") == 422)
    assert refusal["result"]["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    _assert_final_balances(state)
    assert len(state["operations"]) == 3 and len(state["requests"]) == 2
    assert len(state["journals"]) == 2 and len(state["entries"]) == 4
    traces = [entry for actor in demo["actors"] for entry in actor.get("trace", [])]
    lock_trace = [entry for entry in traces if "FOR UPDATE OF b" in entry["sql"]]
    assert lock_trace and all(
        entry["code"]["path"] == "src/ledgerops/db/admission.py"
        and entry["code"]["function"] == "_lock_balances"
        for entry in lock_trace
    )
    assert demo["history"]
    _assert_owned_connections_gone(http_lab, _owned_pids(demo))


def test_http_freeze_waits_for_writer_then_rejects_new_purchase_without_claims(http_lab):
    state = _action(http_lab, "concurrency_start", scenario="freeze_drain")
    assert state["concurrency"]["stage"] == 1
    first_waiter = _actual_waits(http_lab, state["concurrency"])[0]

    state = _action(http_lab, "concurrency_advance")
    demo = state["concurrency"]
    assert demo["stage"] == 2
    waits = _actual_waits(http_lab, demo)
    assert len(waits) == 2
    freeze = next(row for row in waits if "pg_advisory_xact_lock(" in row["query"])
    assert first_waiter["pid"] in freeze["blocking_pids"]
    assert state["workspace"]["mode"] == "NORMAL"

    state = _action(http_lab, "concurrency_advance")
    assert state["concurrency"]["stage"] == 3 and not state["concurrency"]["complete"]
    assert state["workspace"]["mode"] == "FROZEN"
    assert any(actor.get("http_status") == 201 for actor in state["concurrency"]["actors"])
    before = {
        name: state[name] for name in ("operations", "requests", "journals", "entries", "balances")
    }

    state = _action(http_lab, "concurrency_advance")
    demo = state["concurrency"]
    assert demo["stage"] == 4 and demo["complete"]
    assert any(actor.get("http_status") == 503 for actor in demo["actors"])
    assert {name: state[name] for name in before} == before
    _assert_final_balances(state)
    assert len(state["operations"]) == 2 and len(state["requests"]) == 1
    _assert_owned_connections_gone(http_lab, _owned_pids(demo))


@pytest.mark.parametrize("scenario", ["purchase_race", "freeze_drain"])
@pytest.mark.parametrize("stage", [1, 2])
def test_http_refresh_cleanup_closes_waiting_transactions_and_all_owned_connections(
    http_lab, scenario, stage
):
    state = _action(http_lab, "concurrency_start", scenario=scenario)
    if stage == 2:
        state = _action(http_lab, "concurrency_advance")
    pids = _owned_pids(state["concurrency"])
    _actual_waits(http_lab, state["concurrency"])
    state = _action(http_lab, "concurrency_reset")
    assert state["concurrency"] is None or state["concurrency"]["status"] == "cancelled"
    _assert_owned_connections_gone(http_lab, pids)
    # Cancellation, rather than releasing the blocker first, prevents late writes.
    assert len(state["operations"]) == 1
    assert len(state["journals"]) == 1 and not state["requests"]
    assert state["workspace"]["mode"] == "NORMAL" and not state["maintenance_freezes"]
    assert next(row["booked"] for row in state["balances"] if row["label"] == "customer") == "100"


def test_http_global_reset_cleans_concurrency_before_creating_empty_workspace(http_lab):
    state = _action(http_lab, "concurrency_start", scenario="purchase_race")
    pids = _owned_pids(state["concurrency"])
    state = _action(http_lab, "reset")
    _assert_owned_connections_gone(http_lab, pids)
    assert not state["balances"] and not state["operations"]
    assert state["guide"]["index"] == 0 and not state["lab"]["account_opened"]


def test_server_close_cleans_unfinished_workers_before_database_teardown(http_lab):
    _action(http_lab, "concurrency_start", scenario="freeze_drain")
    state = _action(http_lab, "concurrency_advance")
    pids = _owned_pids(state["concurrency"])
    _actual_waits(http_lab, state["concurrency"])
    http_lab.shutdown()
    http_lab.server_close()
    _assert_owned_connections_gone(http_lab, pids)
    state = http_lab.session.state()
    assert state["workspace"]["mode"] == "NORMAL"
    assert not state["maintenance_freezes"] and len(state["operations"]) == 1


def test_abandoned_demo_watchdog_cancels_waiters_without_committing_freeze(http_lab, monkeypatch):
    monkeypatch.setattr(concurrency, "WATCHDOG_SECONDS", 1)
    _action(http_lab, "concurrency_start", scenario="freeze_drain")
    state = _action(http_lab, "concurrency_advance")
    pids = _owned_pids(state["concurrency"])
    timer = http_lab.session.concurrency._timer
    timer.join(timeout=5)  # Wait for the actual cleanup callback, not inferred SQL order.
    assert not timer.is_alive()
    status, state = call(http_lab.origin)
    assert status == 200
    assert state["concurrency"]["status"] == "failed"
    assert state["concurrency"]["error"]["code"] == "SCENARIO_EXPIRED"
    _assert_owned_connections_gone(http_lab, pids)
    assert state["workspace"]["mode"] == "NORMAL"
    assert not state["maintenance_freezes"] and len(state["operations"]) == 1


def test_http_unfinished_demo_blocks_other_writes_and_replacement_cleans_previous(http_lab):
    state = _action(http_lab, "concurrency_start", scenario="purchase_race")
    pids, workspace = _owned_pids(state["concurrency"]), state["workspace"]["id"]
    status, error = call(
        http_lab.origin,
        "/api/action",
        {"action": "purchase", "amount_minor": "80"},
        http_lab.token,
    )
    assert status == 409, error
    state = _action(http_lab, "concurrency_start", scenario="freeze_drain")
    _assert_owned_connections_gone(http_lab, pids)
    assert state["workspace"]["id"] != workspace
    assert state["concurrency"]["kind"] == "freeze_drain"
    assert state["concurrency"]["stage"] == 1
    _actual_waits(http_lab, state["concurrency"])
    _action(http_lab, "concurrency_reset")
