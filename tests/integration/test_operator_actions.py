"""Typed maker/checker actions, actual PostgreSQL effects and retained retry identity."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import errors

from ledgerops import authentication as auth
from ledgerops.db import operator_actions as actions
from ledgerops.db.admission import NotAuthorized, Principal
from ledgerops.db.maintenance import resume_workspace
from ledgerops.db.payout_resolution import blocking_incidents
from ledgerops.domain import ActorId, ClientId, WorkspaceId
from ledgerops.export import capture_snapshot, export_workspace, load_export_bundle
from ledgerops.provider_protocol import ProviderTrust
from ledgerops.reconciliation import StatementTrust, prepare_inputs, publication
from ledgerops.verification import verify_snapshot
from provider_sim.settlement import SettlementStore
from provider_sim.store import ProviderStore
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import NAMESPACE, money
from tests.integration.payout_observation_helpers import (
    event,
    financial_state,
    observe,
    prepared_payout,
    trust,
)
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.test_authentication import ADMIN, provision, revoke
from tests.integration.test_maintenance import ACTOR, _apply, _freeze
from tests.integration.test_purchase_recovery import (
    _runtime_connection_settings,
    _wait_backend_gone,
)
from tests.integration.test_reconciliation_publication import REVISION, accept_all, begin
from tests.provider.test_process import SECRET

pytestmark = pytest.mark.integration
SCOPES = ("approval:propose", "approval:approve", "approval:execute", "evidence:read")


def operator_credential(database, workspace, *, actor=None, client=None, scopes=SCOPES):
    principal = Principal(
        WorkspaceId.parse(str(workspace)),
        ClientId.parse(str(client or uuid4())),
        ActorId.parse(str(actor or uuid4())),
    )
    if actor is None:
        issued = provision(database, principal, scopes=scopes)
    else:
        with database.connect("auth_admin") as conn:
            auth.set_client(
                conn,
                str(workspace),
                str(principal.client_id),
                str(principal.actor_id),
                admin_actor_id=ADMIN,
                reason="second client for stable actor",
            )
            issued = auth.issue_credential(
                conn,
                str(workspace),
                str(principal.client_id),
                scopes,
                admin_actor_id=ADMIN,
                reason="second credential for stable actor",
            )
    with database.connect("operator") as conn:
        return issued, auth.authenticate(conn, issued.token)


def operator_story(database, golden, tmp_path, *, extra_incidents=0, terminal="SUCCEEDED"):
    """100 -> purchase80 -> hold62; paid externally, blocked locally, repaired."""
    command = prepared_payout(database, golden)
    workspace, payout_id = UUID(str(command.workspace_id)), UUID(str(command.operation_id))
    with database.connect("reader") as conn:
        key, raw = conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone()
    provider = ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, "test-v1", SECRET)
    source = (
        provider.close(key, json.loads(raw))
        if terminal == "CLOSED_ABSENT"
        else provider.put(key, json.loads(raw), outcome=terminal)
    )
    signed = json.dumps(source).encode()
    merchant = golden["purchase_request"]["merchant_account_id"]
    with database.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=90 WHERE account_id=%s", (merchant,)
        )
    observed = observe(database, command, signed)
    assert (observed.status, observed.code) == ("BLOCKED", "PROJECTION_MISMATCH")
    with database.connect("owner") as conn:
        for _ in range(extra_incidents):
            conn.execute(
                "INSERT INTO ledger.payout_incident(workspace_id,incident_id,payout_id,category,"
                "code,observation_id) VALUES(%s,%s,%s,'LOCAL_BLOCKER','PROJECTION_MISMATCH',%s)",
                (workspace, uuid4(), payout_id, observed.observation_id),
            )
    freeze = _freeze(database, golden)
    _apply(database, golden, freeze)
    with database.connect("maintenance") as conn:
        resume_workspace(conn, str(workspace), freeze, ACTOR, "repaired local projections")
    assert money(database, golden)[:3] == (80, 62, 18)
    with database.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.payout_incident").fetchone() == (
            1 + extra_incidents,
        )
    settlement = SettlementStore(provider)
    now = datetime.now(UTC).replace(microsecond=0)
    day = now.astimezone(ZoneInfo("Europe/Warsaw")).date().isoformat()
    if terminal == "SUCCEEDED":
        settlement.assign_batch(
            str(uuid4()),
            now.isoformat().replace("+00:00", "Z"),
            [{"row_id": str(uuid4()), "provider_keys": [key]}],
        )
    statement = json.dumps(settlement.statement(str(workspace), day, 1)).encode()
    with database.connect("reader") as conn:
        path = export_workspace(
            conn, str(workspace), tmp_path / "exports", code_revision=REVISION, dirty=True
        )
    bundle = load_export_bundle(path)
    prepared = prepare_inputs(
        bundle,
        statement,
        trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
        code_revision=REVISION,
        dirty=True,
        partitions=1,
    )
    store = publication.ObjectStore(tmp_path / "objects")
    raws = {
        "ledger_manifest": bundle.manifest_bytes,
        "ledger_data": bundle.data_bytes,
        "statement": statement,
    }

    def connect():
        return database.connect("reconciler")

    run = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, run)
    with connect() as conn:
        completed = publication.complete_run(conn, str(workspace), store, prepared, run_id=run)
    assert completed.published
    _, maker = operator_credential(database, workspace)
    _, checker = operator_credential(database, workspace)
    return {
        "database": database,
        "golden": golden,
        "command": command,
        "workspace": workspace,
        "payout_id": payout_id,
        "observation_id": observed.observation_id,
        "evidence_run_id": run,
        "store": store,
        "trust": trust(command),
        "maker": maker,
        "checker": checker,
        "prepared": prepared,
        "raws": raws,
    }


@pytest.fixture
def story(seeded_db, golden, tmp_path):
    return operator_story(seeded_db, golden, tmp_path)


def propose(story, *, action_id=None, credential=None, **changes):
    arguments = {
        "action_id": action_id or uuid4(),
        "payout_id": story["payout_id"],
        "observation_id": story["observation_id"],
        "evidence_run_id": story["evidence_run_id"],
        "reason": "Repaired immutable journal/hold projection; review terminal62",
        "ttl_seconds": 3600,
        "store": story["store"],
        "trust": story["trust"],
        **changes,
    }
    with story["database"].connect("operator") as conn:
        return actions.propose_action(conn, credential or story["maker"], **arguments)


def approve(story, proposal, *, credential=None, **changes):
    with story["database"].connect("operator") as conn:
        return actions.approve_action(
            conn,
            credential or story["checker"],
            **{
                "action_id": UUID(proposal.body["action_id"]),
                "proposal_sha256": proposal.body["proposal_sha256"],
                "store": story["store"],
                "trust": story["trust"],
                **changes,
            },
        )


def execute(story, proposal, *, credential=None, execution_id=None, conn=None, **changes):
    arguments = {
        "action_id": UUID(proposal.body["action_id"]),
        "execution_id": execution_id or uuid4(),
        "proposal_sha256": proposal.body["proposal_sha256"],
        "store": story["store"],
        "trust": story["trust"],
        **changes,
    }
    if conn is not None:
        return actions.execute_action(conn, credential or story["checker"], **arguments)
    with story["database"].connect("operator") as opened:
        return actions.execute_action(opened, credential or story["checker"], **arguments)


def audit_counts(database):
    with database.connect("reader") as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone()[0]
            for table in (
                "operator_action",
                "operator_approval",
                "operator_execution",
                "operator_attempt",
                "payout_incident_resolution",
                "payout_incident",
            )
        )


def test_repair_review_execute_retains_original_incident_and_independently_balances(story):
    database = story["database"]
    proposal = propose(story)
    assert proposal.http_status == 201
    proof = proposal.body["proposal"]
    assert proof["target"]["payout"]["principal_minor"] == "60"
    assert proof["target"]["payout"]["fee_minor"] == "2"
    assert proof["target"]["payout"]["gross_minor"] == "62"
    assert approve(story, proposal).http_status == 201
    execution_id = uuid4()
    result = execute(story, proposal, execution_id=execution_id)
    assert result.http_status == 200 and result.body["state"] == "SUCCEEDED"
    assert not result.replayed
    assert money(database, story["golden"])[:3] == (18, 0, 18)
    assert audit_counts(database) == (1, 1, 1, 1, 1, 1)
    with database.connect("reader") as conn:
        snapshot = capture_snapshot(conn, str(story["workspace"]))
        report = verify_snapshot(snapshot)
        assert report.healthy
        assert report.external_evidence["external_consistency_ok"]
        assert report.external_evidence["payouts_resolved"]
        assert conn.execute(
            "SELECT booked FROM ledger.account_balance b JOIN ledger.account a "
            "ON a.workspace_id=b.workspace_id AND a.id=b.account_id "
            "WHERE a.class='SYNTHETIC_CLEARING'"
        ).fetchone() == (38,)
        assert not blocking_incidents(
            conn.cursor(row_factory=psycopg.rows.dict_row), story["workspace"], story["payout_id"]
        )
    replay = execute(story, proposal, execution_id=execution_id)
    assert replay.replayed and replay.body == result.body
    assert execute(story, proposal).body == result.body
    assert audit_counts(database) == (1, 1, 1, 2, 1, 1)
    with database.connect("operator") as conn:
        state = actions.action_state(
            conn, story["checker"], action_id=UUID(result.body["action_id"])
        )
    assert state["execution"] == result.body and len(state["attempts"]) == 2


def test_proposal_exact_identity_replay_does_not_refresh_expiry_or_stale_evidence(story):
    proposal = propose(story)
    with story["database"].connect("owner") as conn:
        conn.execute("UPDATE ledger.account_balance SET version=version+1")
    replay = propose(story, action_id=UUID(proposal.body["action_id"]))
    assert replay.replayed and replay.body == proposal.body
    with pytest.raises(actions.ActionRejected, match="ACTION_IDENTITY_CONFLICT"):
        propose(story, action_id=UUID(proposal.body["action_id"]), reason="changed reason")


def test_same_actor_with_two_credentials_cannot_check_own_proposal(story):
    proposal = propose(story)
    _, other_token = operator_credential(
        story["database"], story["workspace"], actor=story["maker"].principal.actor_id
    )
    with pytest.raises(auth.PermissionDenied):
        approve(story, proposal, credential=other_token)
    assert audit_counts(story["database"]) == (1, 0, 0, 0, 0, 1)


@pytest.mark.parametrize(
    "mutation", ["maker", "checker", "generation", "target", "epoch", "observation"]
)
def test_execute_rechecks_reviewers_generation_complete_facts_and_target(story, mutation):
    proposal = propose(story)
    approve(story, proposal)
    database = story["database"]
    before = financial_state(database)
    if mutation in {"maker", "checker"}:
        principal = story[mutation].principal
        with database.connect("auth_admin") as conn:
            auth.set_membership(
                conn,
                str(story["workspace"]),
                str(principal.actor_id),
                SCOPES,
                admin_actor_id=ADMIN,
                reason="version changed after review",
                expected_version=1,
            )
    elif mutation == "generation":
        with database.connect("owner") as conn:
            conn.execute("UPDATE ledger.auth_workspace_state SET generation=%s", (uuid4(),))
        # New access after restore is current but the pending action binds old generation.
        issued = None
        with database.connect("auth_admin") as conn:
            issued = auth.issue_credential(
                conn,
                str(story["workspace"]),
                str(story["checker"].principal.client_id),
                SCOPES,
                admin_actor_id=ADMIN,
                reason="fresh restored credential",
            )
        with database.connect("operator") as conn:
            story["checker"] = auth.authenticate(conn, issued.token)
    elif mutation == "target":
        with database.connect("owner") as conn:
            conn.execute("UPDATE ledger.account_balance SET version=version+1")
        before = financial_state(database)
    elif mutation == "epoch":
        with database.connect("owner") as conn:
            conn.execute("UPDATE ledger.recon_registry_guard SET evidence_epoch=evidence_epoch+1")
    else:
        assert (
            observe(
                database, story["command"], event(database, story["command"], sequence=2)
            ).status
            == "BLOCKED"
        )
    execution_id = uuid4()
    result = execute(story, proposal, execution_id=execution_id)
    assert result.http_status == 409
    assert financial_state(database) == before
    assert audit_counts(database) == (1, 1, 0, 1, 0, 1)
    replay = execute(story, proposal, execution_id=execution_id)
    assert replay.replayed and replay.body == result.body


def test_revoked_executor_cannot_even_replay_a_committed_result(story):
    proposal = propose(story)
    approve(story, proposal)
    execution_id = uuid4()
    execute(story, proposal, execution_id=execution_id)
    revoke(story["database"], story["checker"])
    with pytest.raises(auth.AuthenticationRequired):
        execute(story, proposal, execution_id=execution_id)
    assert audit_counts(story["database"]) == (1, 1, 1, 1, 1, 1)


def test_missing_approval_changed_hash_and_attempt_actor_identity_are_retained(story):
    proposal = propose(story)
    execution_id = uuid4()
    rejected = execute(story, proposal, execution_id=execution_id)
    assert rejected.http_status == 422 and rejected.body["code"] == "DISTINCT_APPROVAL_REQUIRED"
    approve(story, proposal)
    assert execute(story, proposal, execution_id=execution_id).body == rejected.body
    wrong = execute(story, proposal, proposal_sha256="00" * 32)
    assert wrong.http_status == 409 and wrong.body["code"] == "PROPOSAL_HASH_CONFLICT"
    with pytest.raises(actions.ActionRejected, match="EXECUTION_IDENTITY_CONFLICT"):
        execute(story, proposal, execution_id=execution_id, credential=story["maker"])
    assert execute(story, proposal).http_status == 200


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_real_lost_commit_retries_same_execution_id_without_fake_failure(story, fault):
    database = story["database"]
    proposal = propose(story)
    approve(story, proposal)
    execution_id = uuid4()
    before = financial_state(database)
    with PostgresCommitProxy(database.port, fault=fault) as proxy:
        settings = _runtime_connection_settings(database, proxy.port)
        settings.update(user="ledger_action_operator", password=database.operator_password)
        with psycopg.connect(**settings) as conn:
            pid = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                execute(story, proposal, execution_id=execution_id, conn=conn)
        assert proxy.finished.wait(5) and proxy.frontend_commit_seen.is_set()
        assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    _wait_backend_gone(database, pid)
    if fault == "before-forward":
        assert financial_state(database) == before
        assert audit_counts(database) == (1, 1, 0, 0, 0, 1)
    result = execute(story, proposal, execution_id=execution_id)
    assert result.http_status == 200 and result.replayed == (fault == "after-commit")
    assert audit_counts(database) == (1, 1, 1, 1, 1, 1)


def test_fault_after_money_before_audit_rolls_back_every_effect(story):
    proposal = propose(story)
    approve(story, proposal)
    database = story["database"]
    before = financial_state(database)
    with database.connect("owner") as conn:
        conn.execute("""CREATE FUNCTION ledger.test_action_fault() RETURNS trigger LANGUAGE plpgsql
          AS $$ BEGIN RAISE EXCEPTION 'injected action audit fault'
          USING ERRCODE='23514'; END $$""")
        conn.execute("""CREATE TRIGGER test_action_fault BEFORE INSERT ON ledger.operator_execution
          FOR EACH ROW EXECUTE FUNCTION ledger.test_action_fault()""")
    execution_id = uuid4()
    with pytest.raises(errors.CheckViolation, match="injected action audit fault"):
        execute(story, proposal, execution_id=execution_id)
    assert financial_state(database) == before
    assert audit_counts(database) == (1, 1, 0, 0, 0, 1)
    with database.connect("owner") as conn:
        conn.execute("DROP TRIGGER test_action_fault ON ledger.operator_execution")
    assert execute(story, proposal, execution_id=execution_id).http_status == 200


def test_thirty_four_blockers_resolve_completely_with_no_summary_limit(seeded_db, golden, tmp_path):
    story = operator_story(seeded_db, golden, tmp_path, extra_incidents=33)
    proposal = propose(story)
    assert len(proposal.body["proposal"]["incident_ids"]) == 34
    approve(story, proposal)
    assert execute(story, proposal).http_status == 200
    assert audit_counts(seeded_db) == (1, 1, 1, 1, 34, 34)
    with seeded_db.connect("reader") as conn:
        assert verify_snapshot(capture_snapshot(conn, str(story["workspace"]))).healthy


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_action_rejects_source_object_loss_without_fallback_or_money(story, failure):
    proposal = propose(story)
    approve(story, proposal)
    reference = story["prepared"].input_manifest["inputs"]["statement"]["sha256"]
    path = story["store"].root / str(story["workspace"]) / reference
    if failure == "missing":
        path.unlink()
    else:
        os.chmod(path, 0o600)
        path.write_bytes(b"x" * path.stat().st_size)
    before = financial_state(story["database"])
    result = execute(story, proposal)
    assert result.http_status == 409 and result.body["code"] in {"MISSING_OBJECT", "CORRUPT_OBJECT"}
    assert financial_state(story["database"]) == before


def test_proposal_expiry_is_rechecked_after_actual_balance_lock_wait(story):
    database = story["database"]
    proposal = propose(story, ttl_seconds=1)
    approve(story, proposal)
    workers = []
    with database.connect("owner") as blocker, database.connect("admin") as observer:
        with database.connect("operator") as executor:
            try:
                with blocker.transaction():
                    blocker.execute("SELECT 1 FROM ledger.account_balance FOR UPDATE")
                    worker = start_call(lambda: execute(story, proposal, conn=executor))
                    workers.append(worker)
                    wait_for_blocker(
                        observer, executor.info.backend_pid, blocker.info.backend_pid, worker
                    )
                    deadline = monotonic() + 3
                    while observer.execute(
                        "SELECT clock_timestamp()>=expires_at FROM ledger.operator_action"
                    ).fetchone() != (True,):
                        assert monotonic() < deadline
                    assert not worker.done.is_set()
                kind, result = finish_call(worker)
                assert kind == "returned", result
                assert result.http_status == 409 and result.body["code"] == "ACTION_EXPIRED"
            finally:
                for pending in workers:
                    if not pending.done.is_set():
                        executor.cancel()
                        finish_call(pending)
    assert money(database, story["golden"])[:3] == (80, 62, 18)


def test_cross_workspace_or_wrong_scope_never_claims_action(story):
    _, weak = operator_credential(story["database"], story["workspace"], scopes=("evidence:read",))
    with pytest.raises(auth.PermissionDenied):
        propose(story, credential=weak)
    fake = replace(
        story["maker"],
        principal=replace(story["maker"].principal, actor_id=ActorId.parse(str(uuid4()))),
    )
    with pytest.raises(auth.AuthenticationRequired):
        propose(story, credential=fake)
    with pytest.raises(NotAuthorized):
        propose(story, payout_id=uuid4())
    assert audit_counts(story["database"]) == (0, 0, 0, 0, 0, 1)


def test_runtime_cannot_replace_audit_or_dispose_provider_contradiction(story):
    proposal = propose(story)
    approve(story, proposal)
    execute(story, proposal)
    database = story["database"]
    action_id = UUID(proposal.body["action_id"])
    with database.connect("operator") as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("UPDATE ledger.operator_action SET action_id=action_id")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("DELETE FROM ledger.operator_action")
    with database.connect("owner") as conn:
        incident = uuid4()
        conn.execute(
            "INSERT INTO ledger.payout_incident(workspace_id,incident_id,payout_id,category,code,"
            "observation_id,related_observation_id,related_payload_sha256) "
            "VALUES(%s,%s,%s,'PROVIDER_CONTRADICTION','EVENT_ID_REUSED',%s,%s,%s)",
            (
                story["workspace"],
                incident,
                story["payout_id"],
                story["observation_id"],
                story["observation_id"],
                bytes(32),
            ),
        )
    with database.connect("operator") as conn, pytest.raises(errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO ledger.payout_incident_resolution(workspace_id,incident_id,payout_id,"
                "action_id,observation_id) VALUES(%s,%s,%s,%s,%s)",
                (
                    story["workspace"],
                    incident,
                    story["payout_id"],
                    action_id,
                    story["observation_id"],
                ),
            )
    assert audit_counts(database) == (1, 1, 1, 1, 1, 2)


@pytest.mark.parametrize("terminal", ["FAILED", "CLOSED_ABSENT"])
def test_reviewed_negative_terminal_releases_hold_without_journal(
    seeded_db, golden, tmp_path, terminal
):
    story = operator_story(seeded_db, golden, tmp_path, terminal=terminal)
    proposal = propose(story)
    approve(story, proposal)
    result = execute(story, proposal)
    assert result.http_status == 200 and result.body["state"] == terminal
    assert result.body["journal_id"] is None
    assert money(seeded_db, golden)[:3] == (80, 0, 80)
    assert audit_counts(seeded_db) == (1, 1, 1, 1, 1, 1)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT state FROM ledger.reservation").fetchone() == ("RELEASED",)
        assert conn.execute(
            "SELECT count(*) FROM ledger.journal WHERE kind='PAYOUT'"
        ).fetchone() == (0,)
        assert verify_snapshot(capture_snapshot(conn, str(story["workspace"]))).healthy


def test_two_concurrent_executions_serialize_to_one_effect(story):
    proposal = propose(story)
    approve(story, proposal)
    database = story["database"]
    pending = []
    with (
        database.connect("owner") as blocker,
        database.connect("operator") as first,
        database.connect("operator") as second,
        database.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                blocker.execute("SELECT 1 FROM ledger.account_balance FOR UPDATE")
                executing = start_call(lambda: execute(story, proposal, conn=first))
                pending.append((first, executing))
                wait_for_blocker(
                    observer,
                    first.info.backend_pid,
                    blocker.info.backend_pid,
                    executing,
                    query_fragment="ledger.account_balance",
                )
                retrying = start_call(lambda: execute(story, proposal, conn=second))
                pending.append((second, retrying))
                wait_for_blocker(
                    observer,
                    second.info.backend_pid,
                    first.info.backend_pid,
                    retrying,
                    query_fragment="ledger.recon_registry_guard",
                )
            results = [finish_call(call) for _, call in pending]
            assert all(
                kind == "returned" and result.http_status == 200 for kind, result in results
            ), results
            assert results[0][1].body == results[1][1].body
        finally:
            for conn, call in pending:
                if not call.done.is_set():
                    conn.cancel()
                    finish_call(call)
    assert money(database, story["golden"])[:3] == (18, 0, 18)
    assert audit_counts(database) == (1, 1, 1, 2, 1, 1)


def related_command(story):
    from tests.integration.helpers import post_fixture, seed_accounts

    workspace = str(story["workspace"])
    second = json.loads(json.dumps(story["golden"]).replace(workspace, str(uuid4())))
    seed_accounts(story["database"], second)
    post_fixture(story["database"], second, "OPENING")
    command = prepared_payout(story["database"], second)
    with story["database"].connect("reader") as conn:
        identifier = conn.execute(
            "SELECT event_id FROM ledger.provider_observation WHERE observation_id=%s",
            (story["observation_id"],),
        ).fetchone()[0]
    return command, event(story["database"], command, event_id=identifier, sequence=2)


def test_related_cross_workspace_contradiction_before_action_prevents_effect(story):
    proposal = propose(story)
    approve(story, proposal)
    command, raw = related_command(story)
    assert observe(story["database"], command, raw).status == "INCIDENT"
    before = financial_state(story["database"])
    result = execute(story, proposal)
    assert result.http_status in {409, 422}
    assert financial_state(story["database"]) == before
    assert audit_counts(story["database"]) == (1, 1, 0, 1, 0, 2)


def test_action_namespace_then_related_callback_and_freeze_have_observed_acyclic_waits(story):
    from ledgerops.db.maintenance import freeze_workspace
    from ledgerops.db.payout_resolution import receive_observation

    proposal = propose(story)
    approve(story, proposal)
    command, raw = related_command(story)
    database = story["database"]
    pending = []
    with (
        database.connect("owner") as blocker,
        database.connect("operator") as executor,
        database.connect("worker") as callback,
        database.connect("maintenance") as maintenance,
        database.connect("admin") as observer,
    ):
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT 1 FROM ledger.account_balance WHERE workspace_id=%s FOR UPDATE",
                    (story["workspace"],),
                )
                executing = start_call(lambda: execute(story, proposal, conn=executor))
                pending.append((executor, executing))
                wait_for_blocker(
                    observer,
                    executor.info.backend_pid,
                    blocker.info.backend_pid,
                    executing,
                    query_fragment="ledger.account_balance",
                )
                observing = start_call(
                    lambda: receive_observation(
                        callback,
                        command.workspace_id,
                        command.operation_id,
                        raw,
                        trust=trust(command),
                    )
                )
                pending.append((callback, observing))
                wait_for_blocker(
                    observer,
                    callback.info.backend_pid,
                    executor.info.backend_pid,
                    observing,
                    query_fragment="pg_advisory_xact_lock",
                )
                freezing = start_call(
                    lambda: freeze_workspace(
                        maintenance,
                        str(story["workspace"]),
                        str(uuid4()),
                        ACTOR,
                        "drain reviewed terminal settlement",
                    )
                )
                pending.append((maintenance, freezing))
                wait_for_blocker(
                    observer,
                    maintenance.info.backend_pid,
                    executor.info.backend_pid,
                    freezing,
                    query_fragment="pg_advisory_xact_lock",
                )
            results = [finish_call(call) for _, call in pending]
            assert all(kind == "returned" for kind, _ in results), results
            assert results[0][1].http_status == 200
            assert results[1][1].status == "INCIDENT"
            with database.connect("reader") as conn:
                assert conn.execute(
                    "SELECT mode FROM ledger.workspace WHERE id=%s", (story["workspace"],)
                ).fetchone() == ("FROZEN",)
            assert money(database, story["golden"])[:3] == (18, 0, 18)
            assert audit_counts(database) == (1, 1, 1, 1, 1, 2)
        finally:
            for conn, call in reversed(pending):
                if not call.done.is_set():
                    conn.cancel()
                    finish_call(call)


def test_complete_incident_review_capacity_rejects_without_partial_disposition(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path, extra_incidents=100)
    with pytest.raises(actions.ActionRejected, match="INCIDENT_REVIEW_CAPACITY") as caught:
        propose(story)
    assert caught.value.http_status == 422
    assert audit_counts(seeded_db) == (0, 0, 0, 0, 0, 101)


@pytest.mark.parametrize("state", ["NOT_FOUND", "ACCEPTED"])
def test_nonterminal_evidence_cannot_release_hold_even_with_local_incident(story, state):
    from tests.integration.payout_observation_helpers import lookup

    database, command = story["database"], story["command"]
    raw = (
        lookup(database, command)
        if state == "NOT_FOUND"
        else event(database, command, "ACCEPTED", sequence=2)
    )
    received = observe(database, command, raw)
    before = financial_state(database)
    with pytest.raises(actions.ActionRejected, match="TERMINAL_OBSERVATION_REQUIRED") as caught:
        propose(story, observation_id=received.observation_id)
    assert caught.value.http_status == 422
    assert financial_state(database) == before
    assert audit_counts(database)[:5] == (0, 0, 0, 0, 0)


def test_human_review_survives_token_rotation_and_history_is_bounded(story):
    proposal = propose(story)
    approve(story, proposal)
    database = story["database"]
    with database.connect("auth_admin") as conn:
        issued = auth.issue_credential(
            conn,
            str(story["workspace"]),
            str(story["checker"].principal.client_id),
            SCOPES,
            admin_actor_id=ADMIN,
            reason="rotate checker credential without grant change",
        )
    revoke(database, story["maker"])
    revoke(database, story["checker"])
    with database.connect("operator") as conn:
        rotated = auth.authenticate(conn, issued.token)
    result = execute(story, proposal, credential=rotated)
    assert result.http_status == 200
    with database.connect("auth_admin") as conn:
        auth.set_membership(
            conn,
            str(story["workspace"]),
            str(story["maker"].principal.actor_id),
            [],
            enabled=False,
            expected_version=1,
            admin_actor_id=ADMIN,
            reason="historical maker disabled after committed effect",
        )
    for _ in range(21):
        replay = execute(story, proposal, credential=rotated)
        assert replay.replayed and replay.body == result.body
    with database.connect("operator") as conn:
        state = actions.action_state(conn, rotated, action_id=UUID(proposal.body["action_id"]))
    assert state["attempts_have_more"] and len(state["attempts"]) == 20
    assert audit_counts(database) == (1, 1, 1, 22, 1, 1)


def test_source_signature_requires_retained_verification_key_at_execution(story):
    proposal = propose(story)
    approve(story, proposal)
    wrong_trust = ProviderTrust(story["command"].provider_namespace, {"test-v1": bytes(32)})
    before = financial_state(story["database"])
    result = execute(story, proposal, trust=wrong_trust)
    assert result.http_status == 422 and result.body["code"] == "SOURCE_AUTHENTICATION_FAILED"
    assert financial_state(story["database"]) == before
