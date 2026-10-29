"""Malformed B bytes cannot bypass independent journal structure/A checks."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_verification import reversal_snapshot as reversal_snapshot
from test_verification import snapshot as snapshot

from ledgerops import stream_verification as stream
from ledgerops.verification import verify_snapshot
from tests.unit.stream_fixture_helpers import source, write


def malformed(data, kind, raw):
    operation = next(row for row in data["operations"] if row["kind"] == kind)
    identity = operation["operation_id"]
    fields = {"payload_hex": raw.hex(), "payload_sha256": hashlib.sha256(raw).hexdigest()}
    operation.update(fields)
    for request in data["requests"]:
        if request["operation_id"] == identity:
            request.update(fields)
    return identity


@pytest.mark.parametrize("kind", ["PURCHASE", "REVERSAL"])
@pytest.mark.parametrize("raw", [b"bad-prefix-no-LF", b"unsupported:v9\n{}"])
@pytest.mark.parametrize("bad_header", [False, True])
def test_malformed_canonical_bytes_keep_own_journal_checked(
    request, tmp_path, kind, raw, bad_header
):
    data = source(
        request.getfixturevalue("reversal_snapshot" if kind == "REVERSAL" else "snapshot")
    )
    identity = malformed(data, kind, raw)
    if bad_header:
        next(row for row in data["journals"] if row["operation_id"] == identity)["total_minor"] = (
            "81"
        )
    legacy = verify_snapshot(data)
    assert not legacy.healthy and not legacy.request_intent_ok
    if bad_header:
        assert not legacy.structure_ok
    result = stream.verify_stream(write(data, tmp_path / "input"))
    assert result["verification_complete"] and not result["healthy"]
    assert not result["request_intent_ok"]
    assert result["projection_ok"] == legacy.projection_ok
    if bad_header:
        assert not result["structure_ok"], result
        assert "JOURNAL_DECLARATION_MISMATCH" in {issue["code"] for issue in result["issues"]}


def test_bad_reversal_canonical_bytes_still_collect_retained_original_and_all_journals(
    reversal_snapshot, tmp_path
):
    data = source(reversal_snapshot)
    identity = malformed(data, "REVERSAL", b"bad-no-LF")
    path = write(data, tmp_path / "input")
    with stream.stage_stream(path) as store:
        closure = stream._closure(store, store.one("operations", "operation_id", identity))
    expected = {row["operation_id"] for row in data["reversal_attempts"]}
    expected |= {row["original_operation_id"] for row in data["reversal_attempts"]}
    assert {row["operation_id"] for row in closure["operations"]} == expected
    assert {row["operation_id"] for row in closure["journals"]} == expected
    assert len(closure["entries"]) == 4
    assert closure["reversal_attempts"] == data["reversal_attempts"]
    assert closure["reversal_effects"] == data["reversal_effects"]


def test_malformed_prefix_and_bad_header_cli_returns_finite_json_without_traceback(
    snapshot, tmp_path
):
    data = source(snapshot)
    identity = malformed(data, "PURCHASE", b"bad-no-LF")
    next(row for row in data["journals"] if row["operation_id"] == identity)["line_count"] = 3
    path = write(data, tmp_path / "input")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    original = Path(stream.__file__)
    for name in (
        "verification.py",
        "verification_external.py",
        "verification_operator.py",
        "stream_contract.py",
    ):
        shutil.copyfile(Path(stream.audit.__file__).parent / name, consumer / name)
    shutil.copyfile(original, consumer / original.name)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(consumer / original.name), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    assert not report["structure_ok"] and not report["healthy"]
