"""Static binding only: CI does not run the fresh held-out refusal sample."""

import hashlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("version", ["refusal-v4", "refusal-v5"])
def test_independent_refusal_sample_is_bound_to_its_manifest(version):
    study = Path(__file__).resolve().parents[1] / "eval" / version
    manifest = json.loads((study / "manifest.json").read_text())
    assert (
        hashlib.sha256((study / "questions.jsonl").read_bytes()).hexdigest()
        == manifest["questions_sha256"]
    )
    assert (
        hashlib.sha256((study / "protocol.json").read_bytes()).hexdigest()
        == manifest["protocol_sha256"]
    )
    rows = [json.loads(line) for line in (study / "questions.jsonl").read_text().splitlines()]
    assert len(rows) == len({q["id"] for q in rows}) == 40
    assert all(q["split"] == "test" for q in rows)
