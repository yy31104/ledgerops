import hashlib
import json
import math

import pytest

from ledgerops_assistant.evaluation import (
    DEFAULT_QUESTIONS,
    check_freeze_record,
    main,
    score_ranking,
)
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


def test_public_manifest_preserves_original_questions_and_split_ids():
    manifest = json.loads(DEFAULT_QUESTIONS.with_name("manifest.json").read_text())
    original_sha = "5c4c6a454d54da93512d56dbd0840cca0a46022d4ac23a79db35a39b4e7695e5"
    assert hashlib.sha256(DEFAULT_QUESTIONS.read_bytes()).hexdigest() == original_sha
    assert manifest["sha256"] == original_sha
    rows = [json.loads(row) for row in DEFAULT_QUESTIONS.read_text().splitlines()]
    assert {split: sum(row["split"] == split for row in rows) for split in ("dev", "test")} == {
        "dev": manifest["dev"],
        "test": manifest["test"],
    }
    check_freeze_record(DEFAULT_QUESTIONS)


@pytest.mark.parametrize("record_name", ["review.json", "manifest.json"])
def test_changed_labels_cannot_silently_reuse_a_frozen_review(tmp_path, record_name):
    questions = tmp_path / "questions.jsonl"
    questions.write_text(DEFAULT_QUESTIONS.read_text())
    questions.with_name(record_name).write_text('{"status":"approved","sha256":"changed"}')
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


@pytest.mark.parametrize("status", ["approved", "frozen_unreviewed"])
def test_legacy_freeze_record_still_works_without_a_public_manifest(tmp_path, status):
    questions = tmp_path / "questions.jsonl"
    questions.write_bytes(DEFAULT_QUESTIONS.read_bytes())
    questions.with_name("review.json").write_text(
        json.dumps({"status": status, "sha256": hashlib.sha256(questions.read_bytes()).hexdigest()})
    )
    check_freeze_record(questions)


def test_changed_manifest_cannot_fall_back_to_an_older_review(tmp_path):
    questions = tmp_path / "questions.jsonl"
    questions.write_bytes(DEFAULT_QUESTIONS.read_bytes())
    questions.with_name("review.json").write_text(
        json.dumps(
            {"status": "approved", "sha256": hashlib.sha256(questions.read_bytes()).hexdigest()}
        )
    )
    questions.with_name("manifest.json").write_text(
        '{"status":"frozen_synthetic","sha256":"changed"}'
    )
    with pytest.raises(ValueError, match="frozen record"):
        check_freeze_record(questions)


def test_strict_model_parser_rejects_extra_keys_and_inconsistent_refusal():
    base = dict(answer="text", citations=["doc#a/0"], refused=False, refusal_reason="none")
    for row in ({**base, "execute": True}, {**base, "refusal_reason": "not_in_docs"}):
        assert parse_draft(json.dumps(row)).refused
