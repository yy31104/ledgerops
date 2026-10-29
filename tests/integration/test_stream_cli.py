"""Installed-style command boundaries: bounded source export, offline healthy/unhealthy exit."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from tests.integration.test_reconciliation_publication import REVISION

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def command(args, environment):
    return subprocess.run(
        [sys.executable, "-m", "ledgerops.cli", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **environment},
        capture_output=True,
        text=True,
        timeout=30,
    )


def export(database, golden, output):
    dsn = make_conninfo(
        host="127.0.0.1",
        port=database.port,
        dbname=database.name,
        user="ledger_reader",
        password=database.reader_password,
    )
    result = command(
        [
            "export",
            "--format",
            "stream10",
            "--workspace",
            golden["workspace"]["id"],
            "--output-root",
            output,
            "--code-revision",
            REVISION,
            "--dirty",
        ],
        {"LEDGEROPS_EXPORT_DSN": dsn},
    )
    assert result.returncode == 0, result.stderr
    assert database.reader_password not in result.stdout + result.stderr
    return Path(json.loads(result.stdout)["export_path"])


def test_stream_cli_exports_then_verifies_without_any_database_connection(
    seeded_db, golden, tmp_path
):
    path = export(seeded_db, golden, tmp_path)
    poison = "host=127.0.0.1 port=1 user=offline password=never-connect-this-secret"
    result = command(["verify", "--format", "stream10", path], {"LEDGEROPS_EXPORT_DSN": poison})
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["healthy"] and report["verification_complete"]
    manifest = json.loads((path / "manifest.json").read_bytes())
    (path / manifest["chunks"][0]["file"]).unlink()
    result = command(["verify", "--format", "stream10", path], {"LEDGEROPS_EXPORT_DSN": poison})
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"] == "EXPORT_OR_VERIFY_FAILED"
    assert "never-connect-this-secret" not in result.stdout + result.stderr


def test_stream_cli_unhealthy_is_distinct_from_unreadable(seeded_db, golden, tmp_path):
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=credits+1 WHERE account_id=%s",
            (golden["purchase_request"]["customer_account_id"],),
        )
    path = export(seeded_db, golden, tmp_path)
    result = command(["verify", "--format", "stream10", path], {})
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["structure_ok"] and report["request_intent_ok"]
    assert not report["projection_ok"] and not report["healthy"]
