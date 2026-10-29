"""Immutable reconciliation publication on real PostgreSQL and owned local files."""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo

from ledgerops.export import export_workspace, load_export_bundle
from ledgerops.reconciliation import StatementTrust, match_partition, prepare_inputs, publication
from provider_sim.settlement import SettlementStore
from provider_sim.store import ProviderStore
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import NAMESPACE
from tests.integration.payout_observation_helpers import (
    financial_state,
    observe,
    prepared_payout,
)
from tests.integration.test_purchase_recovery import _wait_backend_gone
from tests.provider.test_process import SECRET

ROOT = Path(__file__).resolve().parents[2]
REVISION = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
).stdout.strip()


@pytest.fixture
def publication_db(seeded_db):
    return seeded_db, lambda: seeded_db.connect("reconciler")


@pytest.fixture
def inputs(publication_db, golden, tmp_path):
    database, _ = publication_db
    command = prepared_payout(database, golden)
    with database.connect("reader") as conn:
        key, raw = conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone()
    provider = ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, "test-v1", SECRET)
    success = provider.put(key, json.loads(raw))
    assert observe(database, command, json.dumps(success).encode()).status == "APPLIED"
    settlement = SettlementStore(provider)
    instant = datetime.now(UTC).replace(microsecond=0)
    day = instant.astimezone(ZoneInfo("Europe/Warsaw")).date().isoformat()
    settlement.assign_batch(
        str(uuid4()),
        instant.isoformat().replace("+00:00", "Z"),
        [{"row_id": str(uuid4()), "provider_keys": [key]}],
    )
    statement = json.dumps(settlement.statement(str(command.workspace_id), day, 1)).encode()
    with database.connect("reader") as conn:
        path = export_workspace(
            conn,
            str(command.workspace_id),
            tmp_path / "exports",
            code_revision=REVISION,
            dirty=True,
        )
    bundle = load_export_bundle(path)
    prepared = prepare_inputs(
        bundle,
        statement,
        trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
        code_revision=REVISION,
        dirty=True,
        partitions=4,
    )
    return (
        prepared,
        {
            "ledger_manifest": bundle.manifest_bytes,
            "ledger_data": bundle.data_bytes,
            "statement": statement,
        },
        publication.ObjectStore(tmp_path / "objects"),
    )


def begin(connect, prepared, raws, store):
    run_id, publisher_id = uuid4(), uuid4()
    with connect() as conn:
        refs = {
            name: publication.register_object(
                conn, prepared.input_manifest["workspace_id"], store, raw, publisher_id=publisher_id
            )
            for name, raw in raws.items()
        }
        publication.begin_run(conn, store, prepared, run_id=run_id, input_pins=refs)
    return run_id


def part_ref(connect, prepared, store, run_id, part_id, *, change=None):
    result = match_partition(prepared, part_id)
    envelope = publication.partition_envelope(prepared, run_id, result)
    if change:
        change(envelope)
    with connect() as conn:
        return publication.register_object(
            conn,
            prepared.input_manifest["workspace_id"],
            store,
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            publisher_id=uuid4(),
        )


def accept_all(connect, prepared, store, run_id):
    refs = []
    for part in prepared.partition_plan["partitions"]:
        ref = part_ref(connect, prepared, store, run_id, part["partition_id"])
        with connect() as conn:
            assert (
                publication.accept_partition(
                    conn,
                    prepared.input_manifest["workspace_id"],
                    store,
                    run_id=run_id,
                    partition_id=part["partition_id"],
                    result_ref=ref,
                )
                == "ACCEPTED"
            )
        refs.append(ref)
    return refs


def test_complete_fixed_inputs_reader_pin_and_no_financial_mutation(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    before = financial_state(database)
    run_id = begin(connect, prepared, raws, store)
    with database.connect("reader") as conn:
        with pytest.raises(publication.PublicationError, match="RUN_NOT_COMPLETE"):
            publication.pin_run(conn, workspace, store, run_id=run_id, reader_id=uuid4())
    refs = accept_all(connect, prepared, store, run_id)
    with connect() as conn:
        completed = publication.complete_run(conn, workspace, store, prepared, run_id=run_id)
        assert completed.published and completed.evidence_epoch == 1
        assert (
            publication.complete_run(conn, workspace, store, prepared, run_id=run_id) == completed
        )
        assert (
            publication.accept_partition(
                conn, workspace, store, run_id=run_id, partition_id=0, result_ref=refs[0]
            )
            == "ALREADY_ACCEPTED"
        )
    reader = uuid4()
    with database.connect("reader") as conn:
        pinned = publication.pin_run(
            conn,
            workspace,
            store,
            reader_id=reader,
            provider_namespace=NAMESPACE,
            business_date=prepared.input_manifest["business_date"],
        )
        assert pinned.run_id == run_id and pinned.result["reconciled"]
        assert publication.release_reader_pin(conn, workspace, reader_id=reader) > 0
        assert publication.release_reader_pin(conn, workspace, reader_id=reader) == 0
    assert financial_state(database) == before


def test_incomplete_and_failed_runs_never_become_current_and_new_late_output_is_rejected(
    publication_db, inputs
):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    run_id = begin(connect, prepared, raws, store)
    with connect() as conn:
        with pytest.raises(publication.PublicationError, match="INCOMPLETE_PARTITIONS"):
            publication.complete_run(conn, workspace, store, prepared, run_id=run_id)
        assert publication.fail_run(conn, workspace, run_id=run_id, code="WORKER_DIED") == "FAILED"
    ref = part_ref(connect, prepared, store, run_id, 0)
    with connect() as conn:
        with pytest.raises(publication.PublicationError, match="RUN_NOT_STAGING"):
            publication.accept_partition(
                conn, workspace, store, run_id=run_id, partition_id=0, result_ref=ref
            )
        assert conn.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (0,)
        assert conn.execute(
            "SELECT evidence_epoch FROM ledger.recon_registry_guard"
        ).fetchone() == (0,)


def test_cas_loser_is_complete_but_does_not_replace_newer_current(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    first = begin(connect, prepared, raws, store)
    second = begin(connect, prepared, raws, store)
    for run_id in (first, second):
        accept_all(connect, prepared, store, run_id)
    with connect() as conn:
        one = publication.complete_run(conn, workspace, store, prepared, run_id=second)
        two = publication.complete_run(conn, workspace, store, prepared, run_id=first)
        assert one.published and not two.published
        assert one.evidence_epoch == 1 and two.evidence_epoch is None
        assert conn.execute(
            "SELECT run_id,evidence_epoch FROM ledger.recon_current"
        ).fetchone() == (second, 1)
    with database.connect("reader") as conn:
        old = publication.pin_run(conn, workspace, store, reader_id=uuid4(), run_id=first)
        assert old.run_id == first and old.result["reconciled"]


@pytest.mark.parametrize("change", ["run", "generation", "input", "coverage", "semantic"])
def test_wrong_worker_binding_is_not_accepted(publication_db, inputs, change):
    _, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    run_id = begin(connect, prepared, raws, store)
    part_id = next(
        p["partition_id"] for p in prepared.partition_plan["partitions"] if p["coverage"]
    )

    def corrupt(envelope):
        if change == "run":
            envelope["run_id"] = str(uuid4())
        elif change == "generation":
            envelope["generation"] = 2
        elif change == "input":
            envelope["input_manifest_sha256"] = "0" * 64
        elif change == "coverage":
            envelope["coverage"] = envelope["coverage"][:-1]
        else:
            envelope["semantic_sha256"] = "0" * 64

    ref = part_ref(connect, prepared, store, run_id, part_id, change=corrupt)
    with connect() as conn:
        with pytest.raises(publication.PublicationError):
            publication.accept_partition(
                conn, workspace, store, run_id=run_id, partition_id=part_id, result_ref=ref
            )
        assert conn.execute("SELECT count(*) FROM ledger.recon_accepted_result").fetchone() == (0,)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_pinned_old_file_damage_never_falls_back_to_current(publication_db, inputs, damage):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    old = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, old)
    with connect() as conn:
        old_result = publication.complete_run(conn, workspace, store, prepared, run_id=old)
    newer = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, newer)
    with connect() as conn:
        assert publication.complete_run(conn, workspace, store, prepared, run_id=newer).published
    path = store.root / workspace / old_result.result_sha256
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"broken")
    with database.connect("reader") as conn:
        with pytest.raises(publication.PublicationError, match="MISSING_OBJECT|CORRUPT_OBJECT"):
            publication.pin_run(conn, workspace, store, reader_id=uuid4(), run_id=old)
        assert conn.execute("SELECT run_id FROM ledger.recon_current").fetchone() == (newer,)


def test_plan_cannot_omit_or_repeat_physical_rows_before_run_is_created(publication_db, inputs):
    _, connect = publication_db
    prepared, raws, store = inputs
    plan = deepcopy(prepared.partition_plan)
    plan["expected_coverage"].pop()
    changed = replace(prepared, partition_plan=plan)
    with pytest.raises(publication.PublicationError, match="INVALID_PARTITION_PLAN"):
        begin(connect, changed, raws, store)


def test_publisher_cannot_change_financial_records_or_erase_registered_objects(
    publication_db, inputs
):
    database, connect = publication_db
    prepared, raws, store = inputs
    begin(connect, prepared, raws, store)
    before = financial_state(database)
    with connect() as conn:
        for query in (
            "UPDATE ledger.account_balance SET reserved=0",
            "DELETE FROM ledger.journal_entry",
            "DELETE FROM ledger.evidence_pin WHERE owner_type='RUN'",
            "DELETE FROM ledger.recon_object",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(query)
    assert financial_state(database) == before


@pytest.mark.parametrize("same_scope", [True, False])
def test_actual_parallel_cas_serializes_epoch_but_only_compares_its_own_scope(
    publication_db, inputs, tmp_path, same_scope
):
    database, connect = publication_db
    prepared, raws, store = inputs
    second_prepared, second_raws = prepared, raws
    if not same_scope:
        provider = ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, "test-v1", SECRET)
        day = (
            (datetime.fromisoformat(prepared.input_manifest["business_date"]) + timedelta(days=1))
            .date()
            .isoformat()
        )
        second_statement = json.dumps(
            SettlementStore(provider).statement(prepared.input_manifest["workspace_id"], day, 1)
        ).encode()
        bundle = load_export_bundle(next((tmp_path / "exports").iterdir()))
        second_prepared = prepare_inputs(
            bundle,
            second_statement,
            trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
            code_revision=REVISION,
            dirty=True,
            partitions=4,
        )
        second_raws = {**raws, "statement": second_statement}
    ids = [
        begin(connect, p, r, store) for p, r in ((prepared, raws), (second_prepared, second_raws))
    ]
    for identity, p in zip(ids, (prepared, second_prepared), strict=True):
        accept_all(connect, p, store, identity)
    workspace = prepared.input_manifest["workspace_id"]
    calls = []
    with (
        database.connect("owner") as blocker,
        database.connect("admin") as observer,
        connect() as first,
        connect() as second,
    ):
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT workspace_id FROM ledger.recon_registry_guard "
                    "WHERE workspace_id=%s FOR UPDATE",
                    (workspace,),
                )
                for conn, p, identity in zip(
                    (first, second), (prepared, second_prepared), ids, strict=True
                ):
                    call = start_call(
                        lambda c=conn, value=p, identifier=identity: publication.complete_run(
                            c, workspace, store, value, run_id=identifier
                        )
                    )
                    calls.append((conn, call))
                    wait_for_blocker(
                        observer,
                        conn.info.backend_pid,
                        (blocker.info.backend_pid, first.info.backend_pid),
                        call,
                        query_fragment="recon_registry_guard",
                    )
            outcomes = [finish_call(call) for _, call in calls]
            assert all(kind == "returned" for kind, _ in outcomes), outcomes
            assert [value.published for _, value in outcomes] == (
                [True, False] if same_scope else [True, True]
            )
            assert observer.execute(
                "SELECT evidence_epoch FROM ledger.recon_registry_guard"
            ).fetchone() == (1 if same_scope else 2,)
            assert observer.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (
                1 if same_scope else 2,
            )
        finally:
            for conn, call in calls:
                if call.thread.is_alive():
                    conn.cancel()
                    call.thread.join(5)


def test_valid_digest_cannot_hide_invented_group_total(publication_db, inputs):
    from ledgerops.reconciliation import semantic_digest

    _, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    identity = begin(connect, prepared, raws, store)
    changed = []
    for part in prepared.partition_plan["partitions"]:

        def mutate(envelope, partition_id=part["partition_id"]):
            if envelope["result"]["groups"]:
                envelope["result"]["groups"][0]["gross_minor"] = "999"
                envelope["semantic_sha256"] = semantic_digest(envelope["result"])
                changed.append(partition_id)

        ref = part_ref(connect, prepared, store, identity, part["partition_id"], change=mutate)
        with connect() as conn:
            assert (
                publication.accept_partition(
                    conn,
                    workspace,
                    store,
                    run_id=identity,
                    partition_id=part["partition_id"],
                    result_ref=ref,
                )
                == "ACCEPTED"
            )
    assert len(changed) == 1
    with connect() as conn:
        with pytest.raises(publication.PublicationError, match="INVALID_PARTITION_RESULTS"):
            publication.complete_run(conn, workspace, store, prepared, run_id=identity)
        assert conn.execute("SELECT state FROM ledger.recon_run").fetchone() == ("STAGING",)
        assert conn.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (0,)


def kill_publisher(database, connect, workspace, root, phase, *, acceptance=None):
    with connect() as conn:
        dsn = make_conninfo(conn.info.dsn, password=database.reconciler_password)
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(ROOT / "tests/integration/recon_publisher_crash_child.py"),
            phase,
            str(root),
            workspace,
        ],
        env={**os.environ, "RECON_TEST_DSN": dsn, "RECON_TEST_ACCEPTANCE": json.dumps(acceptance)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(10), "publisher failed to reach causal gate"
            line = process.stdout.readline()
        assert line, process.stderr.read().decode()
        state = json.loads(line)
        assert state["phase"] == phase and state["pid"] == process.pid
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        _wait_backend_gone(database, state["backend_pid"])
        return state
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("phase", ["partial_file", "after_rename", "registration_before_commit"])
def test_real_publisher_sigkill_leaves_only_unreferenced_owned_files(
    publication_db, golden, tmp_path, phase
):
    database, connect = publication_db
    workspace = golden["workspace"]["id"]
    root = tmp_path / "crash-objects"
    raw = b"publication-owned-test-bytes\n" * 200
    kill_publisher(database, connect, workspace, root, phase)
    paths = list((root / workspace).iterdir())
    assert len(paths) == 1
    assert paths[0].read_bytes() == (raw[: len(raw) // 2] if phase == "partial_file" else raw)
    assert (
        paths[0].name.startswith(".recon-")
        if phase == "partial_file"
        else paths[0].name == hashlib.sha256(raw).hexdigest()
    )
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM ledger.recon_object").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.evidence_pin").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (0,)
        ref = publication.register_object(
            conn, workspace, publication.ObjectStore(root), raw, publisher_id=uuid4()
        )
        assert ref.sha256 == hashlib.sha256(raw).hexdigest()
        assert conn.execute("SELECT count(*) FROM ledger.recon_object").fetchone() == (1,)


@pytest.mark.parametrize("phase", ["acceptance_before_commit", "acceptance_after_commit"])
def test_real_acceptance_sigkill_never_exposes_a_partial_complete_run(
    publication_db, inputs, phase
):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    identity = begin(connect, prepared, raws, store)
    ref = part_ref(connect, prepared, store, identity, 0)
    kill_publisher(
        database,
        connect,
        workspace,
        store.root,
        phase,
        acceptance={
            "run_id": str(identity),
            "partition_id": 0,
            "sha256": ref.sha256,
            "bytes": ref.bytes,
            "pin_id": str(ref.pin_id),
        },
    )
    committed = phase == "acceptance_after_commit"
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM ledger.recon_accepted_result").fetchone() == (
            int(committed),
        )
        assert conn.execute("SELECT state FROM ledger.recon_run").fetchone() == ("STAGING",)
        assert conn.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (0,)
        assert publication.accept_partition(
            conn, workspace, store, run_id=identity, partition_id=0, result_ref=ref
        ) == ("ALREADY_ACCEPTED" if committed else "ACCEPTED")


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
@pytest.mark.parametrize("stage", ["registration", "completion"])
def test_real_unknown_commit_recovered_by_stable_identity_without_republishing_epoch(
    publication_db, inputs, stage, fault
):
    from tests.integration.postgres_fault_proxy import PostgresCommitProxy

    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    owner = uuid4()
    identity = None
    if stage == "completion":
        identity = begin(connect, prepared, raws, store)
        accept_all(connect, prepared, store, identity)
    with PostgresCommitProxy(database.port, fault=fault) as proxy:
        with psycopg.connect(
            host="127.0.0.1",
            port=proxy.port,
            dbname=database.name,
            user="ledger_reconciler",
            password=database.reconciler_password,
            autocommit=True,
            connect_timeout=2,
            options="-c statement_timeout=10000 -c lock_timeout=5000",
            sslmode="disable",
            gssencmode="disable",
        ) as conn:
            pid = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                if stage == "completion":
                    publication.complete_run(conn, workspace, store, prepared, run_id=identity)
                else:
                    publication.register_object(
                        conn, workspace, store, b"unknown-register", publisher_id=owner
                    )
        assert proxy.finished.wait(5) and proxy.fault_applied.is_set()
        assert proxy.backend_commit_seen.is_set() is (fault == "after-commit")
    _wait_backend_gone(database, pid)
    with connect() as conn:
        if stage == "registration":
            assert conn.execute("SELECT count(*) FROM ledger.recon_object").fetchone() == (
                int(fault == "after-commit"),
            )
            ref = publication.register_object(
                conn, workspace, store, b"unknown-register", publisher_id=owner
            )
            again = publication.register_object(
                conn, workspace, store, b"unknown-register", publisher_id=owner
            )
            assert ref == again
            assert conn.execute("SELECT count(*) FROM ledger.evidence_pin").fetchone() == (1,)
        else:
            expected = "COMPLETE" if fault == "after-commit" else "STAGING"
            assert conn.execute("SELECT state FROM ledger.recon_run").fetchone() == (expected,)
            final = publication.complete_run(conn, workspace, store, prepared, run_id=identity)
            assert final.published and final.evidence_epoch == 1
            assert conn.execute(
                "SELECT evidence_epoch FROM ledger.recon_registry_guard"
            ).fetchone() == (1,)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_object_reader_rejects_nonregular_content_without_blocking(
    publication_db, golden, tmp_path, kind
):
    database, connect = publication_db
    workspace = golden["workspace"]["id"]
    store = publication.ObjectStore(tmp_path / "nonregular")
    raw = b"owned-object"
    with connect() as conn:
        ref = publication.register_object(conn, workspace, store, raw, publisher_id=uuid4())
    path = store.root / workspace / ref.sha256
    path.unlink()
    if kind == "symlink":
        target = tmp_path / "untrusted-target"
        target.write_bytes(raw)
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises(
        publication.PublicationError, match="CORRUPT_OBJECT|OBJECT_STORE_UNAVAILABLE"
    ):
        store.read(workspace, ref.sha256, ref.bytes)


def test_directory_fsync_failure_never_returns_a_registered_reference(
    publication_db, golden, tmp_path, monkeypatch
):
    import stat

    database, connect = publication_db
    workspace = golden["workspace"]["id"]
    store = publication.ObjectStore(tmp_path / "sync-failure")
    original = publication.os.fsync
    failures = []

    def broken(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failures.append(descriptor)
            raise OSError("injected unavailable directory durability capability")
        return original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "fsync", broken)
        with connect() as conn, pytest.raises(OSError, match="injected unavailable directory"):
            publication.register_object(conn, workspace, store, b"no-ref", publisher_id=uuid4())
    assert failures
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM ledger.recon_object").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.evidence_pin").fetchone() == (0,)


def test_reader_cannot_acquire_or_release_publisher_or_run_pins(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    with connect() as conn:
        ref = publication.register_object(
            conn, workspace, store, b"private-publication", publisher_id=uuid4()
        )
    with database.connect("reader") as conn:
        with pytest.raises(errors.InsufficientPrivilege, match="reader may only acquire"):
            conn.execute(
                "INSERT INTO ledger.evidence_pin(workspace_id,pin_id,object_sha256,"
                "owner_type,owner_id) "
                "VALUES(%s,%s,%s,'PUBLISHER',%s)",
                (workspace, uuid4(), ref.sha256, uuid4()),
            )
        with pytest.raises(errors.InsufficientPrivilege, match="reader may only release"):
            conn.execute("DELETE FROM ledger.evidence_pin WHERE pin_id=%s", (ref.pin_id,))
        with pytest.raises(errors.CheckViolation):
            conn.execute("UPDATE ledger.recon_registry_guard SET workspace_id=%s", (uuid4(),))


def test_exact_acceptance_replay_releases_redundant_new_publisher_pin(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    identity = begin(connect, prepared, raws, store)
    refs = accept_all(connect, prepared, store, identity)
    duplicate = part_ref(connect, prepared, store, identity, 0)
    assert duplicate.sha256 == refs[0].sha256 and duplicate.pin_id != refs[0].pin_id
    with connect() as conn:
        assert (
            publication.accept_partition(
                conn, workspace, store, run_id=identity, partition_id=0, result_ref=duplicate
            )
            == "ALREADY_ACCEPTED"
        )
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE pin_id=%s", (duplicate.pin_id,)
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.recon_accepted_result").fetchone() == (4,)


def test_reader_metadata_is_an_at_pin_observation_and_old_run_remains_fixed(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    first = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, first)
    with connect() as conn:
        publication.complete_run(conn, workspace, store, prepared, run_id=first)
    with database.connect("reader") as conn:
        before = publication.pin_run(conn, workspace, store, reader_id=uuid4(), run_id=first)
    assert (before.is_current_at_pin, before.evidence_epoch_at_pin, before.published_epoch) == (
        True,
        1,
        1,
    )
    second = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, second)
    with connect() as conn:
        publication.complete_run(conn, workspace, store, prepared, run_id=second)
    with database.connect("reader") as conn:
        after = publication.pin_run(conn, workspace, store, reader_id=uuid4(), run_id=first)
        current = publication.pin_run(
            conn,
            workspace,
            store,
            reader_id=uuid4(),
            provider_namespace=NAMESPACE,
            business_date=prepared.input_manifest["business_date"],
        )
    assert (before.is_current_at_pin, before.evidence_epoch_at_pin, before.published_epoch) == (
        True,
        1,
        1,
    )
    assert (after.is_current_at_pin, after.evidence_epoch_at_pin, after.published_epoch) == (
        False,
        2,
        1,
    )
    assert (
        current.run_id == second
        and current.is_current_at_pin
        and current.evidence_epoch_at_pin == 2
    )
    assert before.result == after.result == current.result
    assert before.result_sha256 == after.result_sha256 != current.result_sha256
    assert before.semantic_sha256 == after.semantic_sha256 == current.semantic_sha256


@pytest.mark.parametrize("which", ["input", "accepted_partition"])
def test_completion_rechecks_all_source_and_result_bytes_before_becoming_current(
    publication_db, inputs, which
):
    _, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    identity = begin(connect, prepared, raws, store)
    results = accept_all(connect, prepared, store, identity)
    digest = (
        prepared.input_manifest["inputs"]["ledger_data"]["sha256"]
        if which == "input"
        else results[0].sha256
    )
    path = store.root / workspace / digest
    original = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(b"not the pinned bytes")
    with connect() as conn:
        with pytest.raises(publication.PublicationError, match="CORRUPT_OBJECT"):
            publication.complete_run(conn, workspace, store, prepared, run_id=identity)
        assert conn.execute("SELECT state FROM ledger.recon_run").fetchone() == ("STAGING",)
        assert conn.execute("SELECT count(*) FROM ledger.recon_current").fetchone() == (0,)
        path.write_bytes(original)
        path.chmod(0o400)
        assert publication.complete_run(conn, workspace, store, prepared, run_id=identity).published


def test_historical_run_plan_and_acceptances_are_immutable_and_old_pointer_cannot_replay(
    publication_db, inputs
):
    database, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    first = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, first)
    with connect() as conn:
        publication.complete_run(conn, workspace, store, prepared, run_id=first)
    second = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, second)
    with connect() as conn:
        publication.complete_run(conn, workspace, store, prepared, run_id=second)
        with pytest.raises(errors.CheckViolation, match="completed publication epoch"):
            conn.execute("UPDATE ledger.recon_current SET run_id=%s,evidence_epoch=1", (first,))
        with pytest.raises(errors.CheckViolation, match="epoch advances"):
            conn.execute("UPDATE ledger.recon_registry_guard SET evidence_epoch=1")
    with database.connect("owner") as conn:
        with pytest.raises(errors.CheckViolation, match="one terminal transition"):
            conn.execute("UPDATE ledger.recon_run SET manifest=%s", (b"{}",))
        for query in (
            "UPDATE ledger.recon_partition SET generation=1",
            "UPDATE ledger.recon_accepted_result SET semantic_sha256=repeat('0',64)",
            "DELETE FROM ledger.recon_current",
            "DELETE FROM ledger.recon_registry_guard",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(query)
    with connect() as conn:
        assert conn.execute(
            "SELECT run_id,evidence_epoch FROM ledger.recon_current"
        ).fetchone() == (second, 2)


def test_exact_begin_replay_releases_new_publisher_pins_and_accepts_already_consumed_pins(
    publication_db, inputs
):
    _, connect = publication_db
    prepared, raws, store = inputs
    workspace = prepared.input_manifest["workspace_id"]
    identity = begin(connect, prepared, raws, store)
    with connect() as conn:
        duplicate = {
            name: publication.register_object(conn, workspace, store, raw, publisher_id=uuid4())
            for name, raw in raws.items()
        }
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='PUBLISHER'"
        ).fetchone() == (3,)
        assert (
            publication.begin_run(conn, store, prepared, run_id=identity, input_pins=duplicate)
            == identity
        )
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='PUBLISHER'"
        ).fetchone() == (0,)
        # The same previously returned identities remain a valid retry after
        # their transfer has committed and no PUBLISHER pin exists anymore.
        assert (
            publication.begin_run(conn, store, prepared, run_id=identity, input_pins=duplicate)
            == identity
        )
        assert conn.execute("SELECT count(*) FROM ledger.recon_run").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='RUN'"
        ).fetchone() == (3,)
