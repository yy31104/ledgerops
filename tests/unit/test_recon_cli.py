"""The offline command fails promptly on unusable local inputs without printing secrets."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fault", ["fifo", "symlink", "oversize", "missing_keys", "public_keys"])
def test_offline_command_bounds_files_and_masks_configuration_errors(tmp_path, fault):
    secret = "ab" * 32
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"test-v1": secret}))
    keys.chmod(0o600)
    statement = tmp_path / "statement.json"
    if fault == "fifo":
        os.mkfifo(statement)
    elif fault == "symlink":
        target = tmp_path / "source.json"
        target.write_text("{}")
        statement.symlink_to(target)
    elif fault == "oversize":
        with statement.open("wb") as stream:
            stream.seek(16 * 1024 * 1024)
            stream.write(b" ")
    else:
        statement.write_text("{}")
    if fault == "missing_keys":
        keys.unlink()
    elif fault == "public_keys":
        keys.chmod(0o644)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledgerops.recon_cli",
            "match",
            "--ledger-export",
            str(tmp_path),
            "--statement",
            str(statement),
            "--namespace",
            "00000000-0000-4000-8000-000000000100",
            "--keys-file",
            str(keys),
            "--code-revision",
            "a" * 40,
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert json.loads(result.stderr) == {"error": "RECONCILIATION_INPUT_OR_CONFIGURATION_INVALID"}
    assert secret.encode() not in result.stdout + result.stderr
