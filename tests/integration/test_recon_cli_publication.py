"""Actual CLI processes use dedicated roles and fixed evidence bytes."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo

from tests.integration.test_reconciliation_publication import inputs as inputs
from tests.integration.test_reconciliation_publication import publication_db as publication_db
from tests.provider.test_process import SECRET

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def command(args, dsn):
    return subprocess.run(
        [sys.executable, "-m", "ledgerops.recon_cli", *args],
        cwd=ROOT,
        env={**os.environ, "LEDGEROPS_RECON_DSN": dsn},
        capture_output=True,
        timeout=30,
    )


def dsn_for(db, role):
    password = db.admin_password if role == "postgres" else getattr(db, f"{role}_password")
    return make_conninfo(
        host="127.0.0.1",
        port=db.port,
        dbname=db.name,
        user=role if role == "postgres" else f"ledger_{role}",
        password=password,
    )


def arguments(inputs, tmp_path):
    prepared, raws, store = inputs
    export = tmp_path / json.loads(raws["ledger_manifest"])["export_id"]
    export.mkdir()
    (export / "manifest.json").write_bytes(raws["ledger_manifest"])
    (export / "data.json").write_bytes(raws["ledger_data"])
    statement = tmp_path / "statement.json"
    statement.write_bytes(raws["statement"])
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
    keys.chmod(0o600)
    return [
        "publish",
        "--ledger-export",
        str(export),
        "--statement",
        str(statement),
        "--namespace",
        prepared.input_manifest["provider_namespace"],
        "--keys-file",
        str(keys),
        "--objects",
        str(store.root),
        "--workers",
        "2",
        "--partitions",
        "4",
        "--code-revision",
        prepared.input_manifest["code_revision"],
        "--dirty",
    ]


def test_process_publish_read_and_stable_run_retry_leave_no_reader_pin(
    publication_db, inputs, tmp_path
):
    db, _ = publication_db
    prepared, _, store = inputs
    run_id = str(uuid4())
    args = arguments(inputs, tmp_path) + ["--run-id", run_id]
    result = command(args, dsn_for(db, "reconciler"))
    assert result.returncode == 0, result.stderr
    completed = json.loads(result.stdout)["completion"]
    assert completed["run_id"] == run_id and completed["published"]
    assert completed["evidence_epoch"] == 1
    # A caller recovering a lost response keeps the same run and fixed sources.
    again = command(args, dsn_for(db, "reconciler"))
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["completion"] == completed
    read = command(
        [
            "read",
            "--objects",
            str(store.root),
            "--workspace",
            prepared.input_manifest["workspace_id"],
            "--run-id",
            run_id,
        ],
        dsn_for(db, "reader"),
    )
    assert read.returncode == 0, read.stderr
    report = json.loads(read.stdout)
    assert report["result"]["reconciled"]
    assert report["is_current_at_pin"] and report["evidence_epoch_at_pin"] == 1
    with db.connect("reader") as conn:
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='READER'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.recon_run").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='PUBLISHER'"
        ).fetchone() == (0,)
    for secret in (db.reconciler_password.encode(), SECRET.hex().encode()):
        assert secret not in result.stdout + result.stderr + read.stdout + read.stderr


@pytest.mark.parametrize("role", ["app", "reader", "postgres"])
def test_process_rejects_wrong_or_privileged_database_identity(
    publication_db, inputs, tmp_path, role
):
    db, _ = publication_db
    dsn = dsn_for(db, role)
    result = command(arguments(inputs, tmp_path), dsn)
    assert result.returncode == 2 and result.stdout == b""
    assert b"Traceback" not in result.stderr and dsn.encode() not in result.stderr
    with db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.recon_run").fetchone() == (0,)


def test_same_fixed_inputs_and_plan_have_equal_semantics_for_one_two_four_processes(
    publication_db, inputs, tmp_path
):
    db, _ = publication_db
    base = arguments(inputs, tmp_path)
    digests, physical = set(), set()
    for epoch, workers in enumerate((1, 2, 4), 1):
        args = list(base)
        args[args.index("--workers") + 1] = str(workers)
        result = command(args, dsn_for(db, "reconciler"))
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        completion, metrics = output["completion"], output["metrics"]
        assert completion["published"] and completion["evidence_epoch"] == epoch
        assert metrics["children_reaped"] and len(metrics["worker_pids"]) == workers
        digests.add(completion["semantic_sha256"])
        physical.add(completion["result_sha256"])
    assert len(digests) == 1 and len(physical) == 3
