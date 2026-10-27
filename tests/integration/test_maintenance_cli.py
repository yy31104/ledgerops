"""Exercise the complete local maintenance commands using real dedicated credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_cli_freeze_preview_apply_postexport_and_explicit_resume(seeded_db, golden, tmp_path):
    workspace = golden["workspace"]["id"]
    customer = golden["purchase_request"]["customer_account_id"]
    freeze, repair, actor = (str(uuid4()) for _ in range(3))
    with seeded_db.connect("owner") as owner:
        owner.execute(
            "UPDATE ledger.account_balance SET credits=200 WHERE workspace_id=%s AND account_id=%s",
            (workspace, customer),
        )
    common = dict(host="127.0.0.1", port=seeded_db.port, dbname=seeded_db.name, connect_timeout=2)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "LEDGEROPS_MAINTENANCE_DSN": psycopg.conninfo.make_conninfo(
            **common, user="ledger_maintenance", password=seeded_db.maintenance_password
        ),
        "LEDGEROPS_EXPORT_DSN": psycopg.conninfo.make_conninfo(
            **common, user="ledger_reader", password=seeded_db.reader_password
        ),
    }

    def run(*args, environment=env):
        result = subprocess.run(
            [sys.executable, "-m", "ledgerops.cli", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert seeded_db.maintenance_password not in result.stdout + result.stderr
        return json.loads(result.stdout)

    scope = ("--workspace", workspace, "--freeze-id", freeze)
    run("freeze", *scope, "--actor", actor, "--reason", "inspect drift")
    preview = run("preview-rebuild", *scope, "--output-root", str(tmp_path / "preview"))
    assert not preview["report"]["projection_ok"]
    assert preview["report"]["rebuild_eligible"]
    change = next(row for row in preview["changes"] if row["account_id"] == customer)
    assert change["before"]["booked"] == "200" and change["after"]["booked"] == "100"
    repaired = run(
        "apply-rebuild",
        *scope,
        "--repair-id",
        repair,
        "--actor",
        actor,
        "--reason",
        "repair projection",
        "--preview-export-id",
        Path(preview["export_path"]).name,
    )
    assert repaired["old_epoch"] == 1 and repaired["new_epoch"] == 2
    with seeded_db.connect("reader") as reader:
        assert reader.execute(
            "SELECT mode,projection_epoch FROM ledger.workspace WHERE id=%s", (workspace,)
        ).fetchone() == ("FROZEN", 2)
    post = run("export", "--workspace", workspace, "--output-root", str(tmp_path / "post"))
    offline = {key: value for key, value in env.items() if not key.endswith("_DSN")}
    report = run("verify", post["export_path"], environment=offline)
    assert report["healthy"] and report["reservation_supported"]
    resumed = run("resume", *scope, "--actor", actor, "--reason", "post-check passed")
    assert resumed["resumed_by"] == actor
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201

    # A DB connection failure must not emit a credential-bearing exception.
    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledgerops.cli",
            "freeze",
            *scope,
            "--actor",
            actor,
            "--reason",
            "x",
        ],
        cwd=ROOT,
        env={
            **env,
            "LEDGEROPS_MAINTENANCE_DSN": (
                "host=127.0.0.1 port=1 user=private-operator "
                "password=private-maintenance-password connect_timeout=1"
            ),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failed.returncode == 2
    assert "private-operator" not in failed.stderr
    assert "private-maintenance-password" not in failed.stderr
    assert json.loads(failed.stderr)["error"] == "EXPORT_OR_VERIFY_FAILED"
