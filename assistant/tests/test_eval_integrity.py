import json
import math

import pytest

from ledgerops_assistant.evaluation import DEFAULT_QUESTIONS, main, score_ranking
from ledgerops_assistant.providers.base import parse_draft


def test_ndcg_uses_binary_labels_and_unique_sections():
    assert score_ranking(["a", "a", "b"], ["a", "b"])["ndcg@5"] == 1
    expected = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
    assert score_ranking(["x", "a"], ["a", "b"])["ndcg@5"] == pytest.approx(expected)
    assert score_ranking([], [])["ndcg@5"] == 0


def test_draft_is_two_thirds_dev_and_contains_injection_and_no_answer():
    rows = [json.loads(s) for s in DEFAULT_QUESTIONS.read_text().splitlines()]
    assert len(rows) == 60
    assert sum(r["split"] == "test" for r in rows) == 20
    assert sum(r["category"] == "injection" for r in rows) >= 5
    assert any(r["category"] == "no-answer" for r in rows)


def test_changed_labels_cannot_silently_reuse_a_frozen_review(tmp_path):
    questions = tmp_path / "questions.jsonl"
    questions.write_text(DEFAULT_QUESTIONS.read_text())
    questions.with_name("review.json").write_text('{"status":"approved","sha256":"changed"}')
    with pytest.raises(ValueError, match="frozen record"):
        main(
            [
                "--questions",
                str(questions),
                "--split",
                "test",
                "--backend",
                "memory",
                "--embedder",
                "hashing",
            ]
        )


def test_strict_model_parser_rejects_extra_keys_and_inconsistent_refusal():
    base = dict(answer="text", citations=["doc#a/0"], refused=False, refusal_reason="none")
    for row in ({**base, "execute": True}, {**base, "refusal_reason": "not_in_docs"}):
        assert parse_draft(json.dumps(row)).refused
