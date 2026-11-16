"""Static freeze checks only: no evaluation, model or database."""

import hashlib
import json
from collections import Counter

from scripts.evaluate_v11 import STUDY, verify_archive


def test_v11_frozen_questions_protocol_and_archived_source_are_bound():
    assert (
        hashlib.sha256((STUDY / "manifest.json").read_bytes()).hexdigest()
        == "ed7cc87586ced1b7c3cb5a59dc15ae0f246699b1f91ead132cf6f127633c1314"
    )
    manifest = verify_archive()
    rows = [json.loads(line) for line in (STUDY / "questions.jsonl").read_text().splitlines()]
    assert len(rows) == len({row["id"] for row in rows}) == manifest["total"] == 85
    assert Counter(row["expected_route"] for row in rows) == manifest["retained"]
    assert all(row["split"] == "test" for row in rows)
    assert len({row["group"] for row in rows if row["expected_route"] == "docs"}) == 41
