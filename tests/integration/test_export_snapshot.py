"""Actual read-only snapshots, explicit isolation mutation and runnable offline CLI."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from ledgerops import export
from ledgerops.export import ExportError, ExportLimitExceeded, capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.concurrency_helpers import finish_call, start_call
from tests.integration.helpers import insert_operation, post_fixture, seed_accounts
from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mutate_isolation", [False, True], ids=["repeatable-read", "mutant"])
def test_all_tables_share_one_snapshot_even_when_purchase_commits_between_reads(
    seeded_db, golden, mutate_isolation
):
    balances_read, release = Event(), Event()
    changed = []

    class PausingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if mutate_isolation and query == (
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            ):
                query = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY"
                changed.append(True)
            result = super().execute(query, params, **kwargs)
            if isinstance(query, str) and "FROM ledger.account_balance WHERE workspace_id" in query:
                balances_read.set()
                assert release.wait(5), "purchase never released the snapshot reader"
            return result

    with seeded_db.connect("reader") as reader, seeded_db.connect("admin") as observer:
        reader.cursor_factory = PausingCursor
        worker = start_call(lambda: capture_snapshot(reader, golden["workspace"]["id"]))
        try:
            assert balances_read.wait(3), "reader never reached its balance snapshot"
            row = observer.execute(
                "SELECT state,query FROM pg_stat_activity WHERE pid=%s",
                (reader.info.backend_pid,),
            ).fetchone()
            assert row[0] == "idle in transaction" and "account_balance" in row[1]
            result = invoke(seeded_db, *purchase_input(golden))
            assert result.http_status == 201
        finally:
            release.set()
            kind, snapshot = finish_call(worker)
        assert kind == "returned", snapshot
        assert reader.info.transaction_status == TransactionStatus.IDLE
        report = verify_snapshot(snapshot)

        def original_snapshot_assertion():
            assert report.healthy
            assert len(snapshot["operations"]) == 1
            assert len(snapshot["entries"]) == 2
            assert (
                next(
                    r["booked"]
                    for r in snapshot["balances"]
                    if r["account_id"] == golden["purchase_request"]["customer_account_id"]
                )
                == "100"
            )

        if mutate_isolation:
            assert changed == [True]
            assert len(snapshot["entries"]) == 4  # new source against the old projection
            assert not report.projection_ok
            assert any(issue.code == "PROJECTION_DRIFT" for issue in report.issues)
            with pytest.raises(AssertionError):
                original_snapshot_assertion()
        else:
            assert changed == []
            original_snapshot_assertion()
        # A fresh snapshot after that writer commits sees the complete new state.
        reader.cursor_factory = psycopg.Cursor
        assert verify_snapshot(capture_snapshot(reader, golden["workspace"]["id"])).healthy


def test_workspace_filter_covers_every_exported_relation(seeded_db, golden):
    other = copy.deepcopy(golden)
    replacement = "10000000-0000-4000-8000-000000000002"
    other["workspace"]["id"] = replacement
    for collection in (
        "accounts",
        "operations",
        "journals",
        "entries",
        "before_purchase",
        "after_purchase",
    ):
        for row in other[collection]:
            row["workspace_id"] = replacement
    other["purchase_request"]["workspace_id"] = replacement
    seed_accounts(seeded_db, other)
    post_fixture(seeded_db, other, "OPENING")
    invoke(seeded_db, *purchase_input(other))
    with seeded_db.connect("reader") as reader:
        snapshot = capture_snapshot(reader, golden["workspace"]["id"])
    for table in ("accounts", "balances", "operations", "requests", "journals", "entries"):
        assert all(row["workspace_id"] == golden["workspace"]["id"] for row in snapshot[table])
    assert len(snapshot["accounts"]) == 4 and len(snapshot["operations"]) == 1
    assert snapshot["requests"] == []
    assert verify_snapshot(snapshot).healthy


def test_future_schema_is_not_silently_exported_without_its_new_relations(seeded_db, golden):
    with seeded_db.connect("owner") as owner:
        owner.execute(
            "INSERT INTO ledger.schema_migration(version,sha256) VALUES ('003_future.sql','future')"
        )
    with seeded_db.connect("reader") as reader:
        with pytest.raises(ExportError, match="Unsupported database schema"):
            capture_snapshot(reader, golden["workspace"]["id"])
        assert reader.info.transaction_status == TransactionStatus.IDLE


def test_row_limit_fails_instead_of_publishing_a_truncated_bundle(
    seeded_db, golden, tmp_path, monkeypatch
):
    monkeypatch.setattr(export, "MAX_ROWS_PER_TABLE", 3)
    with seeded_db.connect("reader") as reader:
        with pytest.raises(ExportLimitExceeded, match="row limit"):
            export.export_workspace(
                reader,
                golden["workspace"]["id"],
                tmp_path,
                code_revision="328733b871e12c5e4b096efe2db0a25793266123",
                dirty=True,
            )
        assert reader.info.transaction_status == TransactionStatus.IDLE
    assert list(tmp_path.iterdir()) == []


def test_large_database_payload_is_rejected_before_it_is_transferred(
    seeded_db, golden, monkeypatch
):
    with seeded_db.connect("app") as app:
        insert_operation(app, golden["workspace"]["id"], kind="OPENING", payload=b"x" * 2048)
    monkeypatch.setattr(export, "MAX_BUNDLE_BYTES", 1024)
    financial_payload_transferred = []

    class TrackingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if isinstance(query, str) and "SELECT workspace_id,operation_id,kind,payload," in query:
                financial_payload_transferred.append(True)
            return super().execute(query, params, **kwargs)

    with seeded_db.connect("reader") as reader:
        reader.cursor_factory = TrackingCursor
        with pytest.raises(ExportLimitExceeded, match="Payload transfer"):
            capture_snapshot(reader, golden["workspace"]["id"])
        assert reader.info.transaction_status == TransactionStatus.IDLE
    assert not financial_payload_transferred


def test_capture_refuses_an_outer_transaction_without_affecting_it(seeded_db, golden):
    with seeded_db.connect("reader") as reader, reader.transaction():
        with pytest.raises(ExportError, match="idle autocommit"):
            capture_snapshot(reader, golden["workspace"]["id"])
        assert reader.info.transaction_status == TransactionStatus.INTRANS
        assert reader.execute("SELECT 1").fetchone() == (1,)


def test_cli_exports_with_reader_then_verifies_offline_and_masks_connection_details(
    seeded_db, golden, tmp_path
):
    invoke(seeded_db, *purchase_input(golden))
    settings = psycopg.conninfo.make_conninfo(
        host="127.0.0.1",
        port=seeded_db.port,
        dbname=seeded_db.name,
        user="ledger_reader",
        password=seeded_db.reader_password,
        connect_timeout=2,
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "LEDGEROPS_EXPORT_DSN": settings}
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledgerops.cli",
            "export",
            "--workspace",
            golden["workspace"]["id"],
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr
    path = json.loads(created.stdout)["export_path"]
    offline_env = {key: value for key, value in env.items() if key != "LEDGEROPS_EXPORT_DSN"}
    checked = subprocess.run(
        [sys.executable, "-m", "ledgerops.cli", "verify", path],
        cwd=ROOT,
        env=offline_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert checked.returncode == 0, checked.stderr
    report = json.loads(checked.stdout)
    assert report["healthy"] and report["request_intent_ok"]
    assert report["unverified_intent_kinds"] == ["OPENING"]
    secret = "do-not-print-this-password"
    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledgerops.cli",
            "export",
            "--workspace",
            golden["workspace"]["id"],
            "--output-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        env={
            **env,
            "LEDGEROPS_EXPORT_DSN": (
                f"host=127.0.0.1 port=1 user=private-user password={secret} connect_timeout=1"
            ),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failed.returncode == 2
    assert secret not in failed.stdout + failed.stderr
    assert "private-user" not in failed.stderr
    assert json.loads(failed.stderr)["error"] == "EXPORT_OR_VERIFY_FAILED"
