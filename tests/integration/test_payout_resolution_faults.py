"""Real SQL/COMMIT failures, lock waits and exactly one financial terminal effect."""

from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql

from ledgerops.db.admission import RetrySameIdentity
from ledgerops.db.maintenance import freeze_workspace
from ledgerops.db.payout_dispatch import claim_payout, dispatch_once
from ledgerops.db.payout_resolution import receive_observation
from ledgerops.provider_client import TransportResponse
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import NOW, money
from tests.integration.payout_observation_helpers import (
    event,
    financial_state,
    observe,
    prepared_payout,
    raw_table,
    resolution_state,
    trust,
)
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.test_payout_dispatch import Client, next_due, run
from tests.integration.test_payout_resolution import check_final
from tests.integration.test_purchase_recovery import (
    _runtime_connection_settings,
    _wait_backend_gone,
)

pytestmark = pytest.mark.integration

FAULTS = {
    "observation": ("provider_observation", "INSERT", False),
    "journal_entry": ("journal_entry", "INSERT", False),
    "projection": ("account_balance", "UPDATE", False),
    "reservation": ("reservation", "UPDATE", False),
    "hold_event": ("reservation_event", "INSERT", False),
    "resolution": ("payout_resolution", "INSERT", False),
    "intent": ("dispatch_intent", "UPDATE", False),
    "commit": ("payout_resolution", "INSERT", True),
}


@pytest.mark.parametrize("stage", FAULTS)
def test_sql_failure_at_each_resolution_stage_rolls_back_all_sources_and_money_then_retries(
    seeded_db, golden, stage
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    before = resolution_state(seeded_db)
    table, action, deferred = FAULTS[stage]
    with seeded_db.connect("owner") as conn:
        conn.execute("""CREATE FUNCTION ledger.test_resolution_failure() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION USING ERRCODE='23514',
            CONSTRAINT='test_resolution_failure',MESSAGE='injected resolver failure'; END $$""")
        conn.execute(
            sql.SQL(
                "CREATE {constraint}TRIGGER test_resolution_failure AFTER {action} "
                "ON ledger.{table} {deferred}FOR EACH ROW "
                "EXECUTE FUNCTION ledger.test_resolution_failure()"
            ).format(
                constraint=sql.SQL("CONSTRAINT " if deferred else ""),
                action=sql.SQL(action),
                table=sql.Identifier(table),
                deferred=sql.SQL("DEFERRABLE INITIALLY DEFERRED " if deferred else ""),
            )
        )
    with pytest.raises(errors.CheckViolation) as caught:
        observe(seeded_db, command, raw)
    assert caught.value.diag.constraint_name == "test_resolution_failure"
    assert resolution_state(seeded_db) == before
    with seeded_db.connect("owner") as conn:
        conn.execute(
            sql.SQL("DROP TRIGGER test_resolution_failure ON ledger.{}").format(
                sql.Identifier(table)
            )
        )
    assert observe(seeded_db, command, raw).status == "APPLIED"
    check_final(seeded_db, golden, command, "SUCCEEDED")


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_real_unknown_resolution_commit_recovered_by_same_event_has_one_effect(
    seeded_db, golden, fault
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    before = resolution_state(seeded_db)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        settings = _runtime_connection_settings(seeded_db, proxy.port)
        settings.update(user="ledger_worker", password=seeded_db.worker_password)
        with psycopg.connect(**settings) as conn:
            pid = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                receive_observation(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    raw,
                    trust=trust(command),
                    clock=lambda: NOW,
                )
        assert proxy.finished.wait(5) and proxy.frontend_commit_seen.is_set()
        assert proxy.fault_applied.is_set()
        assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    _wait_backend_gone(seeded_db, pid)
    if fault == "before-forward":
        assert resolution_state(seeded_db) == before
    else:
        check_final(seeded_db, golden, command, "SUCCEEDED")
    result = observe(seeded_db, command, raw)
    assert result.status == ("APPLIED" if fault == "before-forward" else "REPLAYED")
    check_final(seeded_db, golden, command, "SUCCEEDED")
    assert len(raw_table(seeded_db, "payout_resolution")) == 1


def test_two_real_concurrent_same_success_events_serialize_and_commit_one_effect(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    workers = []
    with seeded_db.connect("owner") as blocker, seeded_db.connect("admin") as observer:
        with seeded_db.connect("worker") as first, seeded_db.connect("worker") as second:
            try:
                with blocker.transaction():
                    blocker.execute(
                        "SELECT 1 FROM ledger.operation WHERE operation_id=%s FOR UPDATE",
                        (str(command.operation_id),),
                    )
                    worker = start_call(
                        lambda: receive_observation(
                            first,
                            command.workspace_id,
                            command.operation_id,
                            raw,
                            trust=trust(command),
                            clock=lambda: NOW,
                        )
                    )
                    workers.append(worker)
                    wait_for_blocker(
                        observer, first.info.backend_pid, blocker.info.backend_pid, worker
                    )
                    other = start_call(
                        lambda: receive_observation(
                            second,
                            command.workspace_id,
                            command.operation_id,
                            raw,
                            trust=trust(command),
                            clock=lambda: NOW,
                        )
                    )
                    workers.append(other)
                    wait_for_blocker(
                        observer,
                        second.info.backend_pid,
                        (blocker.info.backend_pid, first.info.backend_pid),
                        other,
                    )
                outcomes = [finish_call(w) for w in workers]
                assert all(kind == "returned" for kind, _ in outcomes), outcomes
                assert sorted(result.status for _, result in outcomes) == ["APPLIED", "REPLAYED"]
            finally:
                for conn, worker in zip((first, second), workers, strict=False):
                    if worker.thread.is_alive():
                        conn.cancel()
                        worker.thread.join(5)
    check_final(seeded_db, golden, command, "SUCCEEDED")


def test_frozen_workspace_does_not_acknowledge_or_persist_a_callback(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, str(command.workspace_id), str(uuid4()), str(uuid4()), "inspect")
    before = resolution_state(seeded_db)
    with pytest.raises(RetrySameIdentity):
        observe(seeded_db, command, event(seeded_db, command))
    assert resolution_state(seeded_db) == before


@pytest.mark.parametrize("failure", ["missing", "capacity", "reserved", "booked"])
def test_authoritative_success_with_local_blocker_preserves_hold_and_evidence_without_false_failure(
    seeded_db, golden, failure
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    with seeded_db.connect("owner") as conn:
        if failure == "missing":
            conn.execute(
                "DELETE FROM ledger.account_balance WHERE account_id=%s",
                (str(command.merchant_account_id),),
            )
        elif failure == "capacity":
            conn.execute(
                "UPDATE ledger.account_balance SET version=9223372036854775807 WHERE account_id=%s",
                (str(command.merchant_account_id),),
            )
        elif failure == "booked":
            conn.execute(
                "UPDATE ledger.account_balance SET credits=90 WHERE account_id=%s",
                (str(command.merchant_account_id),),
            )
        else:
            conn.execute(
                "UPDATE ledger.account_balance SET reserved=12 WHERE account_id=%s",
                (str(command.merchant_account_id),),
            )
    before = financial_state(seeded_db)
    result = observe(seeded_db, command, raw)
    assert result.status == "BLOCKED"
    assert financial_state(seeded_db) == before
    assert raw_table(seeded_db, "provider_observation") and raw_table(seeded_db, "payout_incident")
    assert raw_table(seeded_db, "payout_resolution") == []
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT state FROM ledger.reservation").fetchone() == ("HELD",)
        assert conn.execute("SELECT knowledge FROM ledger.payout").fetchone()[0] != "FAILED"


def test_stale_worker_valid_success_still_resolves_after_newer_claim_wins_scheduling(
    seeded_db, golden
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    now = [NOW]
    with seeded_db.connect("worker") as conn:

        def network(action, key, request):
            # The first worker is outside its transaction. A recovered worker can
            # claim after its lease while this response remains in flight.
            with seeded_db.connect("worker") as later:
                replacement = claim_payout(
                    later,
                    command.workspace_id,
                    command.operation_id,
                    clock=lambda: NOW + timedelta(seconds=31),
                )
                assert replacement.action == "LOOKUP"
            now[0] = NOW + timedelta(seconds=32)
            return TransportResponse(200, raw)

        client = Client(conn, network)
        result = dispatch_once(
            conn,
            command.workspace_id,
            command.operation_id,
            client,
            trust=trust(command),
            clock=lambda: now[0],
        )
        assert result.observation.state == "SUCCEEDED"
    check_final(seeded_db, golden, command, "SUCCEEDED")


def test_stale_worker_nonterminal_can_advance_knowledge_but_not_replace_new_claim_schedule(
    seeded_db, golden
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command, "ACCEPTED")
    now, expected_intent = [NOW], []
    with seeded_db.connect("worker") as conn:

        def network(action, key, request):
            with seeded_db.connect("worker") as later:
                assert (
                    claim_payout(
                        later,
                        command.workspace_id,
                        command.operation_id,
                        clock=lambda: NOW + timedelta(seconds=31),
                    ).action
                    == "LOOKUP"
                )
            with seeded_db.connect("reader") as reader:
                expected_intent.append(
                    reader.execute("SELECT * FROM ledger.dispatch_intent").fetchone()
                )
            now[0] = NOW + timedelta(seconds=32)
            return TransportResponse(200, raw)

        result = dispatch_once(
            conn,
            command.workspace_id,
            command.operation_id,
            Client(conn, network),
            trust=trust(command),
            clock=lambda: now[0],
        )
        assert result.observation.state == "ACCEPTED" and result.resolution.status == "OBSERVED"
    with seeded_db.connect("reader") as reader:
        assert (
            reader.execute("SELECT * FROM ledger.dispatch_intent").fetchone() == expected_intent[0]
        )
        assert reader.execute("SELECT knowledge FROM ledger.payout").fetchone() == ("ACCEPTED",)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_local_blocker_remains_sticky_after_projection_repair_and_resume(seeded_db, golden):
    from ledgerops.db.maintenance import resume_workspace
    from tests.integration.test_maintenance import ACTOR, _apply, _freeze

    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=12 WHERE account_id=%s",
            (str(command.merchant_account_id),),
        )
    assert observe(seeded_db, command, raw).status == "BLOCKED"
    freeze = _freeze(seeded_db, golden)
    _apply(seeded_db, golden, freeze)
    with seeded_db.connect("maintenance") as conn:
        resumed = resume_workspace(
            conn, str(command.workspace_id), freeze, ACTOR, "projection fixed"
        )
    assert resumed["resume_report"]["structure_ok"] and resumed["resume_report"]["projection_ok"]
    assert (
        str(command.operation_id)
        in resumed["resume_report"]["external_evidence"]["local_blocker_payout_ids"]
    )
    before = financial_state(seeded_db)
    assert observe(seeded_db, command, raw).status == "BLOCKED"
    assert financial_state(seeded_db) == before
    with seeded_db.connect("worker") as conn:
        assert (
            claim_payout(
                conn,
                command.workspace_id,
                command.operation_id,
                clock=lambda: NOW + timedelta(days=1),
            )
            is None
        )
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_accepted_provider_execution_keeps_lookup_scheduling_after_transport_failure(
    seeded_db, golden, failure
):
    command = prepared_payout(seeded_db, golden)
    assert observe(seeded_db, command, event(seeded_db, command, "ACCEPTED")).status == "OBSERVED"
    with seeded_db.connect("worker") as conn:

        def fail(action, key, request):
            assert action == "LOOKUP"
            if failure == "timeout":
                raise TimeoutError("synthetic lookup timeout")
            return TransportResponse(503, b'{"unavailable":true}')

        client = Client(conn, fail)
        first = run(conn, command, client, next_due(seeded_db))
        assert first.outcome == ("TIMEOUT" if failure == "timeout" else "RESPONSE")
        assert run(conn, command, client, next_due(seeded_db)).claim.action == "LOOKUP"
        assert [action for action, _, _ in client.calls] == ["LOOKUP", "LOOKUP"]
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
