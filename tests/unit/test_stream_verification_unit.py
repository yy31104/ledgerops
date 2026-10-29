"""Pure literal oracles for the disk-backed full financial checker."""

import copy
import json

import pytest
from test_operator_verification import action_snapshot
from test_payout_verification import for_export
from test_verification import snapshot as snapshot

from ledgerops import stream_verification as stream
from ledgerops.export import _json_bytes
from ledgerops.verification import verify_snapshot
from tests.unit.stream_fixture_helpers import source, write


def test_purchase_source_matches_all_legacy_flags(snapshot, tmp_path):
    data = source(snapshot)
    expected = verify_snapshot(data)
    result = stream.verify_stream(write(data, tmp_path / "stream"))
    assert result["healthy"], result
    for flag in (
        "structure_ok",
        "projection_ok",
        "request_intent_ok",
        "reservation_supported",
        "healthy",
        "rebuild_eligible",
    ):
        assert result[flag] == getattr(expected, flag)
    assert result["verification_complete"]


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CLOSED_ABSENT"])
def test_full_operator_terminal_chains_match_legacy_checks(snapshot, tmp_path, state):
    data = action_snapshot(for_export(snapshot), state)
    expected = verify_snapshot(data)
    assert expected.healthy
    result = stream.verify_stream(write(data, tmp_path / "stream"))
    assert result["healthy"] and result["verification_complete"], result
    assert result["external_evidence"]["payouts_resolved"]


def test_beyond_old_row_cap_checks_every_retained_request(snapshot, tmp_path):
    data = source(snapshot)
    row = copy.deepcopy(data["requests"][0])
    for index in range(10001):
        data["requests"].append(dict(row, key=f"extra-{index:05d}"))
    result = stream.verify_stream(write(data, tmp_path / "stream"))
    assert result["healthy"] and result["verification_complete"], result
    data["requests"][-1]["response_body"] = {"wrong": "saved"}
    result = stream.verify_stream(write(data, tmp_path / "corrupt"))
    assert not result["healthy"] and not result["request_intent_ok"]
    assert "REQUEST_OPERATION_MISMATCH" in {issue["code"] for issue in result["issues"]}


def test_projection_and_wrong_owner_mutations_are_independent(snapshot, tmp_path):
    data = source(snapshot)
    data["balances"][0]["debits"] = "99"
    result = stream.verify_stream(write(data, tmp_path / "drift"))
    assert result["structure_ok"] and not result["projection_ok"]
    assert result["request_intent_ok"] and not result["healthy"]
    data = source(snapshot)
    data["entries"][-1]["account_id"] = data["accounts"][-1]["id"]
    result = stream.verify_stream(write(data, tmp_path / "owner"))
    assert not result["healthy"] and not result["request_intent_ok"]


def test_semantic_budget_is_explicitly_incomplete_never_green(snapshot, tmp_path, monkeypatch):
    path = write(source(snapshot), tmp_path / "stream")
    monkeypatch.setattr(stream, "MAX_COHORT", 64)
    report = stream.verify_stream(path)
    assert not report["healthy"] and not report["verification_complete"]
    assert report["unverified_scope_count"] > 0 and report["unverified_scopes"]


def test_missing_chunk_releases_all_scratch_files(snapshot, tmp_path):
    path = write(source(snapshot), tmp_path / "stream")
    manifest = json.loads((path / "manifest.json").read_bytes())
    (path / manifest["chunks"][0]["file"]).unlink()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(stream.StreamError):
        stream.verify_stream(path, scratch_root=scratch)
    assert list(scratch.iterdir()) == []


def test_completed_bytes_in_unpublished_stage_cannot_be_loaded(snapshot, tmp_path):
    path = write(source(snapshot), tmp_path / "published")
    unpublished = path.parent / (".ledgerops-stream-" + path.name + ".tmp")
    path.rename(unpublished)
    with pytest.raises(stream.StreamError, match="COMPLETED_EXPORT_DIRECTORY_REQUIRED"):
        stream.verify_stream(unpublished)


def test_staged_reader_does_not_replace_callers_policy_exception(snapshot, tmp_path):
    path = write(source(snapshot), tmp_path / "published")

    class PolicyRejected(ValueError):
        pass

    with pytest.raises(PolicyRejected, match="caller-owned decision"):
        with stream.stage_stream(path):
            raise PolicyRejected("caller-owned decision")


@pytest.mark.parametrize("mutation", ["major", "row_schema", "schema", "server", "unknown_field"])
def test_modified_manifest_cannot_silently_claim_an_unknown_contract(snapshot, tmp_path, mutation):
    path = write(source(snapshot), tmp_path / "published")
    manifest = json.loads((path / "manifest.json").read_bytes())
    if mutation == "major":
        manifest["format_version"] = 11
    elif mutation == "row_schema":
        manifest["row_format_version"] = 8
    elif mutation == "schema":
        manifest["source"]["schema_versions"] = manifest["source"]["schema_versions"][:-1]
    elif mutation == "server":
        manifest["source"]["server_version"] = "unknown"
    else:
        manifest["future_control"] = True
    body_bytes = sum(chunk["bytes"] for chunk in manifest["chunks"])
    while True:
        raw = _json_bytes(manifest)
        size = body_bytes + len(raw)
        if size == manifest["total_bytes"]:
            break
        manifest["total_bytes"] = size
    (path / "manifest.json").write_bytes(raw)
    with pytest.raises(stream.StreamError):
        stream.verify_stream(path)


def test_child_record_with_valid_id_but_wrong_operation_kind_cannot_escape_closures(
    snapshot, tmp_path
):
    data = source(snapshot)
    template = action_snapshot(for_export(copy.deepcopy(snapshot)))
    extra = copy.deepcopy(template["payouts"][0])
    extra["operation_id"] = next(
        row["operation_id"] for row in data["operations"] if row["kind"] == "PURCHASE"
    )
    data["payouts"].append(extra)
    assert not verify_snapshot(data).healthy
    report = stream.verify_stream(write(data, tmp_path / "wrong-kind"))
    assert not report["healthy"]
    assert not report["structure_ok"] or not report["request_intent_ok"]


def test_actual_sqlite_disk_budget_failure_is_explicit_and_cleans_scratch(
    snapshot, tmp_path, monkeypatch
):
    path = write(source(snapshot), tmp_path / "published")
    connect = stream.sqlite3.connect

    class TinyScratch:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, *args):
            if query == "PRAGMA max_page_count=131072":
                query = "PRAGMA max_page_count=1"
            return self.connection.execute(query, *args)

    monkeypatch.setattr(stream.sqlite3, "connect", lambda *a, **kw: TinyScratch(connect(*a, **kw)))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(stream.StreamError, match="SCRATCH_RESOURCE_LIMIT"):
        stream.verify_stream(path, scratch_root=scratch)
    assert list(scratch.iterdir()) == []
