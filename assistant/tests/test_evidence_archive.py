import copy
import io
import json
import tarfile

import pytest

from scripts import evidence_archive as archive


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    path = root / "assistant/eval/results/record.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"metric": 0.75, "per_question": {"q1": {"answer": "raw"}}}))
    monkeypatch.setattr(archive.subprocess, "check_output", lambda *a, **kw: "a" * 40)
    manifest = archive.pack(root, tmp_path / "release", "evaluation-data-v1")
    return root, tmp_path / "release", manifest


def test_pack_is_reproducible_and_keeps_raws_out_of_summary(evidence, tmp_path):
    root, output, manifest = evidence
    repeated = archive.pack(root, tmp_path / "repeat", "evaluation-data-v1")
    assert repeated == manifest
    assert (output / manifest["asset"]).read_bytes() == (
        tmp_path / "repeat" / repeated["asset"]
    ).read_bytes()
    summary = json.loads((output / "summary.json").read_text())
    assert summary["records"]["assistant/eval/results/record.json"] == {"metric": 0.75}
    assert "raw" in (root / "assistant/eval/results/record.json").read_text()


def test_verified_restore_is_idempotent(evidence, tmp_path):
    _, output, manifest = evidence
    path = output / manifest["asset"]
    expected = ["assistant/eval/results/record.json"]
    cache = tmp_path / "cache"
    assert archive.restore(manifest, cache, path) == expected
    assert archive.restore(manifest, cache, path) == expected
    record = cache / expected[0]
    assert archive.sha256(record.read_bytes()) == manifest["files"][expected[0]]["sha256"]


def test_corrupt_archive_never_writes(evidence, tmp_path):
    _, output, manifest = evidence
    path = output / manifest["asset"]
    path.write_bytes(path.read_bytes() + b"corrupt")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="archive checksum"):
        archive.restore(manifest, cache, path)
    assert not cache.exists()


def test_restore_refuses_modified_cached_file(evidence, tmp_path):
    _, output, manifest = evidence
    cache = tmp_path / "cache"
    archive.restore(manifest, cache, output / manifest["asset"])
    path = cache / next(iter(manifest["files"]))
    path.write_text("modified")
    with pytest.raises(ValueError, match="modified evidence"):
        archive.restore(manifest, cache, output / manifest["asset"])
    assert path.read_text() == "modified"


@pytest.mark.parametrize(
    "name",
    [
        "../secret.json",
        "/tmp/secret.json",
        "assistant/eval/results/../secret.json",
        "assistant/eval/results/./record.json",
        "assistant/eval/results//record.json",
        "assistant/eval/results\\record.json",
        "README.md",
    ],
)
def test_manifest_paths_cannot_escape_cache(evidence, tmp_path, name):
    _, output, manifest = evidence
    manifest = copy.deepcopy(manifest)
    manifest["files"][name] = next(iter(manifest["files"].values()))
    with pytest.raises(ValueError, match="path"):
        archive.restore(manifest, tmp_path / "cache", output / manifest["asset"])


def test_inner_file_digest_checked_even_with_valid_outer_archive(evidence, tmp_path):
    _, output, manifest = evidence
    buffer = io.BytesIO()
    with tarfile.open(output / manifest["asset"], mode="r:gz") as old:
        with tarfile.open(fileobj=buffer, mode="w:gz") as new:
            for member in old.getmembers():
                data = old.extractfile(member).read()
                if member.name != "manifest.json":
                    data = data.replace(b"0.75", b"0.25")
                member.size = len(data)
                new.addfile(member, io.BytesIO(data))
    path = tmp_path / "tampered.tar.gz"
    path.write_bytes(buffer.getvalue())
    manifest = {
        **manifest,
        "archive_sha256": archive.sha256(path.read_bytes()),
        "archive_bytes": path.stat().st_size,
    }
    with pytest.raises(ValueError, match="evidence file checksum"):
        archive.restore(manifest, tmp_path / "cache", path)
    assert not (tmp_path / "cache").exists()


def test_restore_rejects_symlink_destination(evidence, tmp_path):
    _, output, manifest = evidence
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache / "assistant").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        archive.restore(manifest, cache, output / manifest["asset"])
    assert not list(outside.iterdir())
