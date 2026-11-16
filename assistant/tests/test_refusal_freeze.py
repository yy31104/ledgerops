"""Static binding only: CI does not run the fresh held-out refusal sample."""

import hashlib
import json
from pathlib import Path


def test_independent_refusal_sample_is_bound_to_its_manifest():
    study = Path(__file__).resolve().parents[1] / "eval/refusal-v4"
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
