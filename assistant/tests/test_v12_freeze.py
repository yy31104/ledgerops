"""Static freeze checks only: no evaluation, model or database."""

import hashlib
import json
import subprocess
from collections import Counter

from scripts.evaluate_v12 import ROOT, STUDY


def test_v12_frozen_questions_protocol_and_source_are_bound():
    manifest_bytes = (STUDY / "manifest.json").read_bytes()
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == "0046c14a740c0b32b0f4384a969fe00b5d7c8ef9380ce5c28c246c724bad59c2"
    )
    manifest = json.loads(manifest_bytes)
    for name, digest in manifest["inputs_sha256"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    commit = manifest["code_commit"]
    for name, digest in (manifest["code_sha256"] | manifest["corpus_file_sha256"]).items():
        original = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
        assert hashlib.sha256(original).hexdigest() == digest, name
    rows = [json.loads(line) for line in (STUDY / "questions.jsonl").read_text().splitlines()]
    assert len(rows) == len({row["id"] for row in rows}) == manifest["total"]
    assert Counter(row["expected_route"] for row in rows) == manifest["retained"]
    assert all(row["split"] == "test" for row in rows)
    removed = {row["id"] for row in manifest["audit"]["removed"]}
    assert not removed & {row["id"] for row in rows}
