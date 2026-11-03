"""Real PostgreSQL workspace round-robin with genuine capacity-blocked claims."""

import json
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from psycopg.conninfo import make_conninfo

from ledgerops import worker
from ledgerops.db.payout_dispatch import dispatch_once, due_payouts
from ledgerops.export import capture_snapshot
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.provider_protocol import ProviderTrust
from ledgerops.verification import verify_snapshot
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.payout_helpers import (
    NAMESPACE,
    NOW,
    admit,
    fresh_input,
    fund_merchant,
    money,
)
from tests.provider.test_process import SECRET, TOKEN, ProcessProvider, effect_rows

pytestmark = pytest.mark.integration


def workspace_copy(golden, identity):
    return json.loads(json.dumps(golden).replace(golden["workspace"]["id"], str(identity)))


def prepare_workspace(db, golden):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    fund_merchant(db, golden)


def reduce_clearing_to_zero(db, golden):
    # Privileged, explicit historical-capacity loss, after valid admissions.
    # It models old accepted work and keeps every immutable instruction/hold.
    clearing = next(a["id"] for a in golden["accounts"] if a["label"] == "clearing")
    with db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=debits WHERE workspace_id=%s "
            "AND account_id=%s",
            (golden["workspace"]["id"], clearing),
        )


def queue_facts(db, workspace):
    with db.connect("reader") as conn:
        return {
            "payout": conn.execute(
                "SELECT operation_id,knowledge,version FROM ledger.payout "
                "WHERE workspace_id=%s ORDER BY operation_id",
                (workspace,),
            ).fetchall(),
            "intent": conn.execute(
                "SELECT payout_id,state,next_action,next_due_at,attempts,last_attempt_id "
                "FROM ledger.dispatch_intent WHERE workspace_id=%s ORDER BY payout_id",
                (workspace,),
            ).fetchall(),
            "attempts": conn.execute(
                "SELECT count(*) FROM ledger.transport_attempt WHERE workspace_id=%s",
                (workspace,),
            ).fetchone()[0],
        }


def test_twenty_one_blocked_payouts_cannot_hide_another_workspace_in_one_scan(
    db, golden, tmp_path, monkeypatch, capsys
):
    blocked = golden
    healthy = workspace_copy(golden, UUID("f0000000-0000-4000-8000-000000000001"))
    prepare_workspace(db, blocked)
    prepare_workspace(db, healthy)
    commands = []
    for _ in range(21):
        args = fresh_input(blocked, principal_minor="1")
        assert admit(db, blocked, args).http_status == 202
        commands.append(args[1])
    ready = fresh_input(healthy)
    assert admit(db, healthy, ready).http_status == 202
    reduce_clearing_to_zero(db, blocked)
    before = queue_facts(db, blocked["workspace"]["id"])
    assert before["attempts"] == 0 and money(db, blocked)[:3] == (80, 63, 17)

    provider = ProcessProvider(tmp_path, namespace=NAMESPACE)
    try:
        provider.start()
        config = worker.WorkerConfig(
            make_conninfo(
                host="127.0.0.1",
                port=db.port,
                dbname=db.name,
                user="ledger_worker",
                password=db.worker_password,
            ),
            provider.ready["url"],
            TOKEN,
            ProviderTrust(ready[1].provider_namespace, {"test-v1": SECRET}),
            batch_size=20,
        )
        original_due, original_dispatch = worker.due_payouts, worker.dispatch_once
        scans = []

        def fixed_due(conn, **kwargs):
            result = original_due(conn, **kwargs, clock=lambda: NOW)
            scans.append(result)
            return result

        monkeypatch.setattr(worker, "due_payouts", fixed_due)
        monkeypatch.setattr(
            worker,
            "dispatch_once",
            lambda *args, **kwargs: original_dispatch(*args, **kwargs, clock=lambda: NOW),
        )
        assert worker.run(config, Event(), once=True) == 2  # capacity skips remain visible
        assert len(scans) == 1 and len(scans[0]) == 20
        assert (ready[1].workspace_id.value, ready[1].operation_id.value) in scans[0]
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert sum(e["event"] == "payout_attempt" for e in events) == 1
        assert sum(e["event"] == "payout_blocked" for e in events) == 19
        assert effect_rows(provider.path) == [
            (f"v1.{NAMESPACE}.{ready[1].workspace_id}.{ready[1].operation_id}", "60", "2", "62")
        ]
        assert money(db, healthy)[:3] == (18, 0, 18)
        assert queue_facts(db, blocked["workspace"]["id"]) == before
        with db.connect("reader") as reader:
            assert verify_snapshot(capture_snapshot(reader, healthy["workspace"]["id"])).healthy

        # Expiry changes the action to CLOSE even while clearing has no capacity.
        # Every original hold releases only after its authenticated tombstone.
        client = ProviderHTTPClient(provider.ready["url"], TOKEN)
        with db.connect("worker") as conn:
            for command in commands:
                result = dispatch_once(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    client,
                    trust=config.trust,
                    clock=lambda: NOW + timedelta(seconds=61),
                )
                assert result.claim.action == "CLOSE"
                assert result.observation.state == "CLOSED_ABSENT"
                late = client.put(result.claim.provider_key, result.claim.request_bytes)
                assert json.loads(json.loads(late.body)["payload"])["state"] == "CLOSED_ABSENT"
        assert money(db, blocked)[:3] == (80, 0, 80)
        assert len(effect_rows(provider.path)) == 1
    finally:
        provider.stop()


def test_cursor_rotates_more_than_one_batch_of_permanently_blocked_workspaces(
    db, golden, monkeypatch, capsys
):
    identities = [UUID(f"10000000-0000-4000-8000-{index:012x}") for index in range(1, 26)]
    for identity in identities:
        sample = workspace_copy(golden, identity)
        prepare_workspace(db, sample)
        assert admit(db, sample, fresh_input(sample)).http_status == 202
        reduce_clearing_to_zero(db, sample)
    visited, cursors, pauses = [], [], []
    original_due, original_dispatch = worker.due_payouts, worker.dispatch_once

    def fixed_due(conn, **kwargs):
        candidates = original_due(conn, **kwargs, clock=lambda: NOW)
        assert len(candidates) == 20
        cursors.append(kwargs["after_workspace"])
        visited.extend(workspace for workspace, _ in candidates)
        return candidates

    stop = Event()

    def next_scan(pause):
        # No timing decides order: two real scans, then explicit shutdown.
        pauses.append(pause)
        if len(pauses) == 2:
            stop.set()

    monkeypatch.setattr(worker, "due_payouts", fixed_due)
    monkeypatch.setattr(
        worker,
        "dispatch_once",
        lambda *args, **kwargs: original_dispatch(*args, **kwargs, clock=lambda: NOW),
    )
    monkeypatch.setattr(stop, "wait", next_scan)
    config = worker.WorkerConfig(
        make_conninfo(
            host="127.0.0.1",
            port=db.port,
            dbname=db.name,
            user="ledger_worker",
            password=db.worker_password,
        ),
        "http://127.0.0.1:9",
        TOKEN,
        ProviderTrust(fresh_input(golden)[1].provider_namespace, {"test-v1": SECRET}),
        batch_size=20,
    )
    assert worker.run(config, stop) == 0
    assert cursors == [None, identities[19]]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    blocked = [row for row in events if row["event"] == "payout_blocked"]
    assert len(blocked) == 40
    assert {row["reason"] for row in blocked} == {"settlement_capacity_unavailable"}
    assert set(visited) == set(identities)
    assert visited[:20] == identities[:20]
    assert visited[20:25] == identities[20:25]
    with db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (0,)
        assert conn.execute("SELECT DISTINCT knowledge FROM ledger.payout").fetchall() == [
            ("NOT_SENT",)
        ]


@pytest.mark.parametrize("bad_cursor", ["not-a-uuid", uuid4().hex, True])
def test_round_robin_cursor_requires_a_typed_workspace_identity(db, bad_cursor):
    with db.connect("worker") as conn, pytest.raises(TypeError):
        due_payouts(conn, after_workspace=bad_cursor)
