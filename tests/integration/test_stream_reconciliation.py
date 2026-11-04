"""Complete stream authority reaches reconciliation publication and typed action execution."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from ledgerops.db import operator_actions as actions
from ledgerops.db.payout_resolution import receive_observation
from ledgerops.reconciliation import StatementTrust
from ledgerops.reconciliation.publication import pin_run, release_reader_pin
from ledgerops.reconciliation.statement import CorruptInput, canonical
from ledgerops.stream_export import export_stream
from ledgerops.stream_reconciliation import (
    KIND,
    build_compact,
    load_pinned_compact,
    run_stream_reconciliation,
)
from ledgerops.stream_verification import verify_stream
from tests.integration.payout_helpers import NAMESPACE, admit, payout_input
from tests.integration.payout_observation_helpers import trust
from tests.integration.test_operator_actions import approve, execute, operator_story, propose
from tests.integration.test_reconciliation_publication import REVISION
from tests.provider.test_process import SECRET

pytestmark = pytest.mark.integration


def source(story, root):
    database, golden = story["database"], story["golden"]
    second = payout_input(
        golden, key="capacity-second", operation_id=str(uuid4()), principal_minor="1"
    )
    assert admit(database, golden, second).http_status == 202
    with database.connect("worker") as conn:
        for number in range(129):
            command = story["command"] if number < 65 else second[1]
            raw = number.to_bytes(2, "big") + b"x" * 65534
            assert (
                receive_observation(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    raw,
                    trust=trust(command),
                    channel="CALLBACK",
                ).status
                == "QUARANTINED"
            )
    with database.connect("owner") as conn:
        conn.execute(
            "INSERT INTO ledger.request_result(workspace_id,client_id,route_family,key,"
            "operation_id,payload,payload_sha256,http_status,response_body) "
            "SELECT workspace_id,client_id,route_family,'bulk-'||g::text,operation_id,"
            "payload,payload_sha256,http_status,response_body FROM ledger.request_result "
            "CROSS JOIN generate_series(1,10005) g WHERE operation_id=%s",
            (story["payout_id"],),
        )
    with database.connect("reader") as conn:
        path = export_stream(
            conn, str(story["workspace"]), root, code_revision=REVISION, dirty=True
        )
    return path


def publish(story, path):
    with story["database"].connect("reconciler") as conn:
        result = run_stream_reconciliation(
            conn,
            story["store"],
            path,
            story["raws"]["statement"],
            trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
            code_revision=REVISION,
            dirty=True,
            partitions=1,
        )
    assert result.completion.published
    return {**story, "evidence_run_id": result.completion.run_id}, result


def test_full_pinned_stream_129_payloads_and_10006_keys_support_r4_and_action(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    path = source(story, tmp_path / "stream")
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["row_counts"]["observation_quarantines"] == 129
    assert manifest["row_counts"]["requests"] > 10000
    compact, raw = build_compact(path)
    assert compact["verification"]["verification_complete"]
    assert compact["verification"]["request_intent_ok"]
    assert len(raw) < 16 * 1024 * 1024
    updated, result = publish(story, path)
    reader = uuid4()
    with seeded_db.connect("reconciler") as conn:
        pinned = pin_run(
            conn,
            str(story["workspace"]),
            story["store"],
            reader_id=reader,
            run_id=result.completion.run_id,
        )
        assert pinned.manifest["source_kind"] == KIND
        assert len(pinned.manifest["inputs"]) == 3 + len(manifest["chunks"])
        assert len(
            [
                identity
                for identity in pinned.partition_plan["expected_coverage"]
                if ":stream_chunk:" in identity
            ]
        ) == len(manifest["chunks"])
        release_reader_pin(conn, str(story["workspace"]), reader_id=reader)
    proposal = propose(updated)
    assert approve(updated, proposal).http_status == 201
    assert execute(updated, proposal).http_status == 200
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
            (golden["purchase_request"]["merchant_account_id"],),
        ).fetchone() == (18, 3)
        final = export_stream(
            conn, str(story["workspace"]), tmp_path / "after", code_revision=REVISION
        )
    report = verify_stream(final)
    assert report["verification_complete"] and report["structure_ok"]
    assert report["projection_ok"] and report["request_intent_ok"]
    assert report["external_evidence"]["accepted_payout_count"] == 2
    assert report["external_evidence"]["resolved_payout_count"] == 1


def test_missing_opaque_quarantine_chunk_stops_action_without_old_run_fallback(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    path = source(story, tmp_path / "stream")
    updated, result = publish(story, path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    chunk = next(row for row in manifest["chunks"] if row["table"] == "observation_quarantines")
    (story["store"].root / str(story["workspace"]) / chunk["sha256"]).unlink()
    with pytest.raises((actions.ActionRejected, RuntimeError)):
        propose(updated)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.operator_action").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
        assert conn.execute("SELECT run_id FROM ledger.recon_current").fetchone() == (
            result.completion.run_id,
        )


def test_compact_facts_and_report_cannot_replace_complete_source_authority(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    path = source(story, tmp_path / "stream")
    compact, raw = build_compact(path)
    compact["targets"]["balances"][0]["booked"] = "999"
    with pytest.raises(CorruptInput, match="COMPACT_SOURCE_REPORT_MISMATCH"):
        load_pinned_compact(
            canonical(compact),
            (path / "manifest.json").read_bytes(),
            lambda chunk: (path / chunk["file"]).read_bytes(),
        )


def test_full_stream_request_contradiction_remains_diagnostic_and_cannot_authorize_action(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    source(story, tmp_path / "valid-stream")
    with seeded_db.connect("admin") as conn:
        original = conn.execute(
            "SELECT payload FROM ledger.request_result WHERE operation_id=%s AND key='payout-60'",
            (story["payout_id"],),
        ).fetchone()[0]
        wrong = original.replace(b'"principal_minor":"60"', b'"principal_minor":"61"')
        assert wrong != original
        conn.execute("ALTER TABLE ledger.request_result DISABLE TRIGGER USER")
        try:
            conn.execute(
                "UPDATE ledger.request_result SET payload=%s,payload_sha256=sha256(%s) "
                "WHERE operation_id=%s AND key='payout-60'",
                (wrong, wrong, story["payout_id"]),
            )
        finally:
            conn.execute("ALTER TABLE ledger.request_result ENABLE TRIGGER USER")
    with seeded_db.connect("reader") as conn:
        path = export_stream(
            conn, str(story["workspace"]), tmp_path / "bad-stream", code_revision=REVISION
        )
    compact, _ = build_compact(path)
    assert compact["verification"]["verification_complete"]
    assert not compact["verification"]["request_intent_ok"]
    updated, result = publish(story, path)
    with pytest.raises(actions.ActionRejected, match="UNSAFE_EVIDENCE_SOURCE"):
        propose(updated)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.operator_action").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)


def test_unsupported_full_verification_never_becomes_compact_matcher_authority(
    seeded_db, golden, tmp_path, monkeypatch
):
    from ledgerops import stream_verification as consumer

    story = operator_story(seeded_db, golden, tmp_path / "story")
    with seeded_db.connect("reader") as conn:
        path = export_stream(
            conn, str(story["workspace"]), tmp_path / "stream", code_revision=REVISION
        )
    monkeypatch.setattr(consumer, "MAX_COHORT", 1)
    report = verify_stream(path)
    assert report["verification_complete"] is False and report["healthy"] is False
    with pytest.raises(CorruptInput, match="INCOMPLETE_STREAM_VERIFICATION"):
        build_compact(path)


def test_actual_reconciliation_cli_stream_match_publish_and_stable_run_id(
    seeded_db, golden, tmp_path
):
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    story = operator_story(seeded_db, golden, tmp_path / "story")
    path = source(story, tmp_path / "stream")
    statement = tmp_path / "statement.json"
    statement.write_bytes(story["raws"]["statement"])
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
    keys.chmod(0o600)
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "LEDGEROPS_RECON_DSN": make_conninfo(
            host="127.0.0.1",
            port=seeded_db.port,
            dbname=seeded_db.name,
            user="ledger_reconciler",
            password=seeded_db.reconciler_password,
        ),
    }
    common = [
        "--source-format",
        "stream10",
        "--ledger-export",
        str(path),
        "--statement",
        str(statement),
        "--namespace",
        NAMESPACE,
        "--keys-file",
        str(keys),
        "--code-revision",
        REVISION,
        "--dirty",
    ]

    def run(command, extra=()):
        result = subprocess.run(
            [sys.executable, "-m", "ledgerops.recon_cli", command, *common, *extra],
            env=environment,
            capture_output=True,
            timeout=60,
        )
        assert seeded_db.reconciler_password.encode() not in result.stdout + result.stderr
        return result

    matched = run("match")
    assert matched.returncode == 1, matched.stderr.decode()
    report = json.loads(matched.stdout)
    assert report["usable"] and report["inputs"]["source_kind"] == KIND
    identity = uuid4()
    extra = ["--objects", str(story["store"].root), "--partitions", "1", "--run-id", str(identity)]
    first = run("publish", extra)
    assert first.returncode == 0, first.stderr.decode()
    first_result = json.loads(first.stdout)
    with seeded_db.connect("reader") as conn:
        before = conn.execute("SELECT evidence_epoch FROM ledger.recon_registry_guard").fetchone()
        pin_count = conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='RUN'"
        ).fetchone()
    second = run("publish", extra)
    assert second.returncode == 0, second.stderr.decode()
    assert json.loads(second.stdout)["completion"] == first_result["completion"]
    with seeded_db.connect("reader") as conn:
        assert (
            conn.execute("SELECT evidence_epoch FROM ledger.recon_registry_guard").fetchone()
            == before
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='RUN'"
            ).fetchone()
            == pin_count
        )
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='PUBLISHER'"
        ).fetchone() == (0,)
    unsupported = run("publish", [*extra, "--workers", "2"])
    assert unsupported.returncode == 2 and b"STREAM_SERIAL_WORKER_REQUIRED" in unsupported.stderr


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_stream_wrapper_real_commit_ambiguity_reports_unknown_and_same_run_recovers(
    seeded_db, golden, tmp_path, monkeypatch, fault
):
    import psycopg

    from ledgerops.reconciliation import publication
    from ledgerops.reconciliation.runner import RunError
    from tests.integration.postgres_fault_proxy import PostgresCommitProxy

    story = operator_story(seeded_db, golden, tmp_path / "story")
    with seeded_db.connect("reader") as conn:
        path = export_stream(
            conn, str(story["workspace"]), tmp_path / "stream", code_revision=REVISION, dirty=True
        )
    identity = uuid4()
    original = publication.begin_run
    with (
        seeded_db.connect("reconciler") as direct,
        PostgresCommitProxy(seeded_db.port, fault=fault) as proxy,
    ):
        with psycopg.connect(
            host="127.0.0.1",
            port=proxy.port,
            dbname=seeded_db.name,
            user="ledger_reconciler",
            password=seeded_db.reconciler_password,
            autocommit=True,
            sslmode="disable",
            gssencmode="disable",
            options="-c statement_timeout=10000 -c lock_timeout=5000",
        ) as uncertain:

            class Routed:
                current = direct

                def __getattr__(self, name):
                    return getattr(self.current, name)

            routed = Routed()

            def lose_begin(conn, *args, **kwargs):
                conn.current = uncertain
                return original(conn, *args, **kwargs)

            monkeypatch.setattr(publication, "begin_run", lose_begin)
            with pytest.raises(RunError) as caught:
                run_stream_reconciliation(
                    routed,
                    story["store"],
                    path,
                    story["raws"]["statement"],
                    trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
                    code_revision=REVISION,
                    dirty=True,
                    partitions=1,
                    run_id=identity,
                )
            error = caught.value
            assert error.run_id == identity and error.outcome_unknown and not error.failure_recorded
            assert proxy.frontend_commit_seen.wait(2)
            assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    monkeypatch.setattr(publication, "begin_run", original)
    with seeded_db.connect("reader") as conn:
        state = conn.execute(
            "SELECT state FROM ledger.recon_run WHERE run_id=%s", (identity,)
        ).fetchone()
        assert state == (("STAGING",) if fault == "after-commit" else None)
        assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
    with seeded_db.connect("reconciler") as conn:
        result = run_stream_reconciliation(
            conn,
            story["store"],
            path,
            story["raws"]["statement"],
            trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
            code_revision=REVISION,
            dirty=True,
            partitions=1,
            run_id=identity,
        )
        assert result.completion.run_id == identity and result.completion.published
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ledger.recon_run WHERE run_id=%s", (identity,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT evidence_epoch FROM ledger.recon_registry_guard"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='PUBLISHER'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"source_kind": "FUTURE_UNSUPPORTED"}, "UNSUPPORTED_EVIDENCE_SOURCE_KIND"),
        ({"manifest_version": 99}, "UNSUPPORTED_EVIDENCE_SOURCE_VERSION"),
    ],
)
def test_valid_legacy_financial_bytes_cannot_hide_unknown_action_evidence_capability(
    seeded_db, golden, tmp_path, change, code
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    with seeded_db.connect("admin") as conn:
        manifest = json.loads(
            conn.execute(
                "SELECT manifest FROM ledger.recon_run WHERE run_id=%s", (story["evidence_run_id"],)
            ).fetchone()[0]
        )
        manifest.update(change)
        conn.execute("ALTER TABLE ledger.recon_run DISABLE TRIGGER USER")
        try:
            conn.execute(
                "UPDATE ledger.recon_run SET manifest=%s WHERE run_id=%s",
                (canonical(manifest), story["evidence_run_id"]),
            )
        finally:
            conn.execute("ALTER TABLE ledger.recon_run ENABLE TRIGGER USER")
    with pytest.raises(actions.ActionRejected, match=code):
        propose(story)
