"""Real subprocess bounds for the administrator's native PostgreSQL tool adapter."""

import io
import sys
import time

import pytest

from ledgerops.backup import BackupError, PgTools


def test_native_tool_output_and_password_environment_are_bounded():
    secret = "local-tool-password-not-an-argument"
    tool = PgTools({"PGPASSWORD": secret})
    output = io.BytesIO()
    tool._run(
        (
            sys.executable,
            "-c",
            "import os,sys; assert 'PGPASSWORD' in os.environ; "
            "sys.stdout.buffer.write(b'custom-dump')",
        ),
        database="synthetic",
        output=output,
        maximum=64,
    )
    assert output.getvalue() == b"custom-dump"
    assert secret not in repr(tool)


@pytest.mark.parametrize(
    "command,limit,error",
    [
        ("import sys; sys.exit(3)", 100, "PG_TOOL_FAILED"),
        ("import sys; sys.stdout.buffer.write(b'x'*65536)", 32, "PG_TOOL_OUTPUT_LIMIT"),
        ("import time; time.sleep(5)", 100, "PG_TOOL_TIMEOUT"),
    ],
)
def test_native_tool_failure_output_limit_and_total_deadline(command, limit, error):
    tool = PgTools({}, timeout_seconds=1)
    started = time.monotonic()
    with pytest.raises(BackupError, match=error):
        tool._run((sys.executable, "-c", command), database="synthetic", maximum=limit)
    assert time.monotonic() - started < 3
