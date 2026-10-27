"""Filesystem contract tests; database snapshot behavior is exercised separately."""

import hashlib
import json
import os
from copy import deepcopy
from uuid import UUID

import pytest

from ledgerops import export

WORKSPACE = "10000000-0000-4000-8000-000000000001"
ACCOUNT = "30000000-0000-4000-8000-000000000001"
REVISION = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def snapshot():
    return {
        "format_version": 1,
        "workspace": {"id": WORKSPACE, "currency": "PLN", "schema_epoch": 1, "mode": "NORMAL"},
        "snapshot_witness": "10:20:",
        "schema_versions": ["001_foundation.sql", "002_export_reader.sql"],
        "server_version": "180006",
        "accounts": [
            {
                "workspace_id": WORKSPACE,
                "id": ACCOUNT,
                "owner_id": "20000000-0000-4000-8000-000000000001",
                "class": "SYNTHETIC_CLEARING",
                "normal_side": "D",
                "currency": "PLN",
                "enabled": True,
            }
        ],
        "balances": [
            {
                "workspace_id": WORKSPACE,
                "account_id": ACCOUNT,
                "normal_side": "D",
                "debits": "0",
                "credits": "0",
                "reserved": "0",
                "booked": "0",
                "version": 0,
            }
        ],
        "operations": [],
        "requests": [],
        "journals": [],
        "entries": [],
    }


@pytest.fixture
def publish(tmp_path, monkeypatch, snapshot):
    # Capture substitution isolates the filesystem contract. It establishes no
    # PostgreSQL claim; integration tests use actual reader credentials.
    monkeypatch.setattr(export, "capture_snapshot", lambda conn, workspace: deepcopy(snapshot))

    def run(**options):
        return export.export_workspace(None, WORKSPACE, tmp_path, code_revision=REVISION, **options)

    return run


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )


def rewrite_data(directory, data=None, raw=None):
    if raw is None:
        raw = (
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    (directory / "data.json").write_bytes(raw)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["data_bytes"] = len(raw)
    manifest["data_sha256"] = hashlib.sha256(raw).hexdigest()
    write_json(directory / "manifest.json", manifest)


def test_bundle_roundtrip_preserves_raw_snapshot_and_manifest_provenance(publish, snapshot):
    directory = publish(dirty=True)
    assert str(UUID(directory.name)) == directory.name
    assert {member.name for member in directory.iterdir()} == {"data.json", "manifest.json"}
    assert export.load_export(directory) == snapshot
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert manifest["code_revision"] == REVISION and manifest["dirty"] is True
    assert manifest["row_counts"] == {
        "workspace": 1,
        "accounts": 1,
        "balances": 1,
        "operations": 0,
        "requests": 0,
        "journals": 0,
        "entries": 0,
    }
    assert manifest["data_bytes"] == (directory / "data.json").stat().st_size
    assert (
        manifest["data_sha256"]
        == hashlib.sha256((directory / "data.json").read_bytes()).hexdigest()
    )
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "data.json").stat().st_mode & 0o777 == 0o600


def test_new_exports_never_overwrite_existing_completed_bundle(publish):
    first = publish()
    original = {p.name: p.read_bytes() for p in first.iterdir()}
    second = publish()
    assert second != first
    assert {p.name: p.read_bytes() for p in first.iterdir()} == original


def test_collision_fails_and_preserves_original(publish, monkeypatch):
    fixed = UUID("90000000-0000-4000-8000-000000000001")
    monkeypatch.setattr(export, "uuid4", lambda: fixed)
    first = publish()
    original = {p.name: p.read_bytes() for p in first.iterdir()}
    with pytest.raises(export.ExportError, match="already exists"):
        publish()
    assert {p.name: p.read_bytes() for p in first.iterdir()} == original


@pytest.mark.parametrize("fault", ["data-fsync", "rename"])
def test_failure_before_publish_leaves_no_accepted_final_bundle(
    publish, monkeypatch, tmp_path, fault
):
    if fault == "data-fsync":

        def fail(*args):
            raise OSError("injected data fsync failure")

        monkeypatch.setattr(export.os, "fsync", fail)
    else:

        def fail(*args):
            raise OSError("injected rename failure")

        monkeypatch.setattr(export.os, "rename", fail)
    with pytest.raises(OSError, match="injected"):
        publish()
    directories = list(tmp_path.iterdir())
    assert len(directories) == 1 and directories[0].name.startswith(".ledgerops-export-")
    with pytest.raises(export.ExportError):
        export.load_export(directories[0])


def test_failure_after_rename_can_leave_a_valid_completed_bundle(
    publish, monkeypatch, tmp_path, snapshot
):
    original = export._sync_directory

    def fail_parent(path):
        if path == tmp_path:
            raise OSError("injected parent fsync failure")
        original(path)

    monkeypatch.setattr(export, "_sync_directory", fail_parent)
    with pytest.raises(OSError, match="injected parent"):
        publish()
    (directory,) = tmp_path.iterdir()
    assert export.load_export(directory) == snapshot


@pytest.mark.parametrize("change", ["missing", "extra", "directory-symlink", "member-symlink"])
def test_incomplete_unknown_or_symlink_members_are_not_accepted(publish, tmp_path, change):
    directory = publish()
    if change == "missing":
        (directory / "data.json").unlink()
    elif change == "extra":
        (directory / "unknown.json").write_text("{}")
    elif change == "member-symlink":
        target = tmp_path / "outside.json"
        target.write_bytes((directory / "data.json").read_bytes())
        (directory / "data.json").unlink()
        (directory / "data.json").symlink_to(target)
    else:
        target = tmp_path / "other-parent"
        target.mkdir()
        link = target / directory.name
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    with pytest.raises(export.ExportError):
        export.load_export(directory)


def test_changed_bytes_without_digest_update_are_rejected(publish):
    directory = publish()
    with (directory / "data.json").open("ab") as output:
        output.write(b" ")
    with pytest.raises(export.ExportError, match="size/digest"):
        export.load_export(directory)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", True),
        ("format_version", 2),
        ("dirty", 1),
        ("data_bytes", True),
        ("data_sha256", "0" * 63),
        ("code_revision", "not-a-commit"),
        ("export_id", "90000000-0000-4000-8000-000000000099"),
        ("workspace_id", "10000000-0000-4000-8000-000000000099"),
    ],
)
def test_manifest_types_versions_and_identity_are_checked(publish, field, value):
    directory = publish()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest[field] = value
    write_json(directory / "manifest.json", manifest)
    with pytest.raises(export.ExportError):
        export.load_export(directory)


@pytest.mark.parametrize("value", [True, 2])
def test_declared_row_counts_cannot_hide_actual_membership(publish, value):
    directory = publish()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["row_counts"]["accounts"] = value
    write_json(directory / "manifest.json", manifest)
    with pytest.raises(export.ExportError, match="row counts"):
        export.load_export(directory)


@pytest.mark.parametrize(
    "fault", ["scope", "float-money", "bool-version", "schema", "extra", "duplicate-row"]
)
def test_rehashed_bundle_still_requires_correct_snapshot_shape(publish, snapshot, fault):
    directory = publish()
    altered = deepcopy(snapshot)
    if fault == "scope":
        altered["accounts"][0]["workspace_id"] = "10000000-0000-4000-8000-000000000099"
    elif fault == "float-money":
        altered["balances"][0]["booked"] = 0.0
    elif fault == "bool-version":
        altered["balances"][0]["version"] = False
    elif fault == "schema":
        altered["schema_versions"].append("003_future.sql")
    elif fault == "extra":
        altered["unknown"] = []
    else:
        altered["accounts"].append(deepcopy(altered["accounts"][0]))
    rewrite_data(directory, altered)
    with pytest.raises(export.ExportError):
        export.load_export(directory)


@pytest.mark.parametrize("raw", [b'{"format_version":1,"format_version":1}', b'{"bad":NaN}'])
def test_duplicate_keys_and_nonfinite_json_are_rejected(publish, raw):
    directory = publish()
    rewrite_data(directory, raw=raw)
    with pytest.raises(export.ExportError, match="Invalid bundle JSON"):
        export.load_export(directory)


def test_row_limit_fails_before_publication_instead_of_truncating(
    publish, snapshot, monkeypatch, tmp_path
):
    snapshot["accounts"].append(deepcopy(snapshot["accounts"][0]))
    monkeypatch.setattr(export, "MAX_ROWS_PER_TABLE", 1)
    with pytest.raises(export.ExportLimitExceeded, match="row limit"):
        publish()
    assert not list(tmp_path.iterdir())


def test_total_bundle_limit_includes_both_data_and_manifest(
    publish, snapshot, monkeypatch, tmp_path
):
    data_bytes = len(
        (
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    monkeypatch.setattr(export, "MAX_BUNDLE_BYTES", data_bytes + 10)
    with pytest.raises(export.ExportLimitExceeded, match="Complete bundle"):
        publish()
    assert not list(tmp_path.iterdir())


def test_loader_bounds_file_reads(publish, monkeypatch):
    directory = publish()
    monkeypatch.setattr(export, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(export.ExportLimitExceeded, match="byte limit"):
        export.load_export(directory)


def test_nonregular_member_is_rejected_without_waiting_for_a_writer(publish):
    directory = publish()
    (directory / "data.json").unlink()
    os.mkfifo(directory / "data.json")
    with pytest.raises(export.ExportError, match="regular files"):
        export.load_export(directory)
