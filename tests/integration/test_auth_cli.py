"""Administrator CLI provisions real credentials without putting tokens in logs."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo

from ledgerops.authentication import authenticate
from tests.integration.purchase_helpers import purchase_input

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
ADMIN = "95000000-0000-4000-8000-000000000001"


def invoke(db, arguments, *, role="ledger_identity_admin", password=None):
    dsn = make_conninfo(
        host="127.0.0.1",
        port=db.port,
        dbname=db.name,
        user=role,
        password=db.auth_admin_password if password is None else password,
    )
    result = subprocess.run(
        [sys.executable, "-m", "ledgerops.auth_cli", *arguments],
        cwd=ROOT,
        env={**os.environ, "LEDGEROPS_AUTH_ADMIN_DSN": dsn},
        capture_output=True,
        timeout=15,
    )
    assert dsn.encode() not in result.stdout + result.stderr
    return result


def test_real_cli_bootstrap_issue_protected_file_revoke(seeded_db, golden, tmp_path):
    principal, _, _ = purchase_input(golden)
    common = [
        "--workspace",
        str(principal.workspace_id),
        "--admin-actor",
        ADMIN,
        "--reason",
        "CLI smoke",
    ]
    actor = ["--actor", str(principal.actor_id)]
    client = ["--client", str(principal.client_id)]
    assert (
        invoke(seeded_db, ["membership", *common, *actor, "--scope", "purchase:write"]).returncode
        == 0
    )
    assert invoke(seeded_db, ["client", *common, *actor, *client]).returncode == 0
    output = tmp_path / "token.json"
    issued = invoke(
        seeded_db, ["issue", *common, *client, "--scope", "purchase:write", "--output", str(output)]
    )
    assert issued.returncode == 0, issued.stderr
    metadata = json.loads(issued.stdout)
    secret = json.loads(output.read_text())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert secret["token"].encode() not in issued.stdout + issued.stderr
    assert metadata["credential_id"] == secret["credential_id"]
    with seeded_db.connect("service") as conn:
        assert authenticate(conn, secret["token"]).principal == principal
    refused = invoke(
        seeded_db, ["issue", *common, *client, "--scope", "purchase:write", "--output", str(output)]
    )
    assert refused.returncode == 2 and json.loads(output.read_text()) == secret
    with seeded_db.connect("owner") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.auth_credential").fetchone() == (1,)
    revoked = invoke(seeded_db, ["revoke", *common, "--credential", metadata["credential_id"]])
    assert revoked.returncode == 0


@pytest.mark.parametrize("role", ["ledger_service", "postgres"])
def test_wrong_and_privileged_logins_cannot_bootstrap_identity(seeded_db, golden, role):
    principal, _, _ = purchase_input(golden)
    password = seeded_db.service_password if role == "ledger_service" else seeded_db.admin_password
    result = invoke(
        seeded_db,
        [
            "membership",
            "--workspace",
            str(principal.workspace_id),
            "--actor",
            str(principal.actor_id),
            "--admin-actor",
            ADMIN,
            "--reason",
            "wrong role",
            "--scope",
            "purchase:write",
        ],
        role=role,
        password=password,
    )
    assert result.returncode == 2 and b"Traceback" not in result.stderr
    with seeded_db.connect("owner") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.auth_membership").fetchone() == (0,)
