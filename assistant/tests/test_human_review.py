import copy
import csv
import json
import sys
from contextlib import contextmanager
from dataclasses import asdict

import pytest

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import ANSWER_SCHEMA, SYSTEM_PROMPT, Draft, user_message
from scripts import export_human_review as export
from scripts import review_current_answers as sample


class Provider:
    name = "chatgpt_subscription"
    model = sample.MODEL

    def __init__(self, output, *, fail=None, interrupt=None, calibration_error=False, missing=None):
        self.output = output
        self.fail, self.interrupt, self.missing = fail, interrupt, missing
        self.calibration_error = calibration_error
        self.calibration_calls, self.questions = [], []

    def generate(self, instructions, message, schema):
        assert instructions == SYSTEM_PROMPT
        assert schema == ANSWER_SCHEMA
        if message != user_message("", []):
            row = json.loads(self.output.read_text())["answers"][len(self.questions)]
            passages = [Chunk(**source) for source in row["provided_sources"]]
            assert message == user_message(row["question"], passages)
            draft = self.draft(row["question"], passages)
            return json.dumps(
                {
                    "answer": draft.answer,
                    "citations": draft.citations,
                    "refused": draft.refused,
                    "refusal_reason": draft.refusal_reason,
                }
            ), draft.usage
        self.calibration_calls.append((instructions, message, schema))
        if self.calibration_error:
            raise TimeoutError("private exception detail must not enter the record")
        return '{"refused":true}', {
            "input_tokens": 9000,
            "output_tokens": 12,
            "cached_input_tokens": 4000,
        }

    def draft(self, question, passages):
        self.questions.append(question)
        persisted = json.loads(self.output.read_text())
        index = len(self.questions) - 1
        assert persisted["answers"][index]["status"] == "running"
        assert persisted["answers"][index]["generation_status"] == "attempted"
        assert persisted["answers"][index]["provided_sources"] == [asdict(c) for c in passages]
        assert persisted["calibration"]["status"] in {"recorded", "error"}
        if len(self.questions) == self.interrupt:
            raise KeyboardInterrupt
        if len(self.questions) == self.fail:
            raise TimeoutError("private exception detail must not enter the record")
        return Draft(
            "Example answer for human judgement.",
            [passages[0].chunk_id],
            usage={
                "input_tokens": None if len(self.questions) == self.missing else 10000,
                "output_tokens": 42,
                "cached_input_tokens": 4500,
            },
        )


class Retriever:
    name = "scripted"

    def __init__(self, source):
        self.source = source

    def context(self, query, k):
        return [self.source]


@pytest.fixture
def questions(corpus):
    return sample.original_questions(corpus.section_ids())


@pytest.fixture
def source():
    return Chunk(
        "docs/PAYOUTS.md#rule/0",
        "docs/PAYOUTS.md#rule",
        "docs/PAYOUTS.md",
        "Payouts",
        "Rule",
        "Payouts > Rule\n\nPreserve the documented condition.",
    )


def session(provider, source):
    @contextmanager
    def factory():
        yield provider, Retriever(source), None

    return factory


def collect(tmp_path, questions, source, **kwargs):
    output = tmp_path / "sample.json"
    provider = Provider(output, **kwargs)
    record = sample.collect_review(
        output,
        questions,
        {"corpus_revision": "a" * 40, "retrieval_method": "vector"},
        session(provider, source),
    )
    return record, provider, output


def test_original_32_cohort_stays_separate_from_expanded_development(questions):
    assert [row["id"] for row in questions] == sample.COHORT_IDS
    assert len(questions) == 32 and all(row["expected_route"] == "docs" for row in questions)


def test_single_calibration_and_all_32_answers_remain_unrated(tmp_path, questions, source):
    record, provider, output = collect(tmp_path, questions, source)
    assert record["status"] == "completed"
    assert len(provider.calibration_calls) == 1 and len(provider.questions) == 32
    assert record["calibration"]["cohort_member"] is False
    assert record["human_review"]["status"] == "pending"
    assert record["billed_usd"] is None
    assert all(row["judgement"] == "" for row in record["answers"])
    assert all(row["provided_sources"] == [asdict(source)] for row in record["answers"])
    for row in record["answers"]:
        payload = row["payload"]
        assert payload["message_utf8_bytes"] == (
            payload["context_utf8_bytes"]
            + payload["question_utf8_bytes"]
            + payload["message_framing_utf8_bytes"]
        )
        assert payload["instruction_utf8_bytes"] == len(SYSTEM_PROMPT.encode())
        assert row["result"]["usage"]["cached_input_tokens"] == 4500
        assert row["generation"]["usage"] == row["result"]["usage"]
        assert json.loads(row["generation"]["raw_response"])["answer"] == row["result"]["text"]
    assert record["token_contrast"]["mean_full_minus_empty_input_tokens"] == 1000
    assert record["token_contrast"]["complete"]
    assert "exact" in record["token_contrast"]["interpretation"]
    assert json.loads(output.read_text()) == record


def test_existing_run_is_never_overwritten_or_sent_to_a_provider(tmp_path, questions, source):
    output = tmp_path / "sample.json"
    output.write_text("original")
    provider = Provider(output)
    with pytest.raises(FileExistsError):
        sample.collect_review(
            output, questions, {"corpus_revision": "a" * 40}, session(provider, source)
        )
    assert output.read_text() == "original"
    assert not provider.calibration_calls and not provider.questions


def test_one_question_failure_is_preserved_without_retry(tmp_path, questions, source):
    record, provider, output = collect(tmp_path, questions, source, fail=3)
    assert record["status"] == "partial" and len(provider.questions) == 32
    row = record["answers"][2]
    assert row["status"] == "error" and row["error_type"] == "TimeoutError"
    assert row["provided_sources"] == [asdict(source)]
    assert row["generation_status"] == "attempted"
    assert "private exception detail" not in output.read_text()
    assert record["token_contrast"]["reported_full_calls"] == 31
    assert record["token_contrast"]["mean_full_minus_empty_input_tokens"] is None


def test_calibration_failure_is_not_retried_or_hidden(tmp_path, questions, source):
    record, provider, _ = collect(tmp_path, questions, source, calibration_error=True)
    assert record["status"] == "partial"
    assert len(provider.calibration_calls) == 1 and len(provider.questions) == 32
    assert record["calibration"]["status"] == "error"
    assert record["token_contrast"]["empty_evidence_input_tokens"] is None
    assert record["token_contrast"]["mean_full_minus_empty_input_tokens"] is None


def test_missing_usage_is_unknown_instead_of_zero(tmp_path, questions, source):
    record, _, _ = collect(tmp_path, questions, source, missing=4)
    assert record["status"] == "completed"
    assert record["answers"][3]["result"]["usage"]["input_tokens"] is None
    assert not record["token_contrast"]["complete"]
    assert record["token_contrast"]["reported_full_calls"] == 31
    assert record["token_contrast"]["mean_full_minus_empty_input_tokens"] is None


def test_malformed_model_output_and_its_usage_remain_in_raw_record(tmp_path, questions, source):
    output = tmp_path / "sample.json"

    class MalformedProvider(Provider):
        def generate(self, instructions, message, schema):
            raw, usage = super().generate(instructions, message, schema)
            if message != user_message("", []) and len(self.questions) == 1:
                raw = "not structured JSON"
            return raw, usage

    provider = MalformedProvider(output)
    record = sample.collect_review(
        output,
        questions,
        {"corpus_revision": "a" * 40},
        session(provider, source),
    )
    row = record["answers"][0]
    assert row["generation"]["raw_response"] == "not structured JSON"
    assert row["generation"]["usage"]["input_tokens"] == 10000
    assert row["result"]["refused"] and row["judgement"] == ""
    assert record["token_contrast"]["reported_full_calls"] == 32


def test_interruption_keeps_the_attempt_and_unstarted_cohort_rows(tmp_path, questions, source):
    output = tmp_path / "sample.json"
    provider = Provider(output, interrupt=3)
    with pytest.raises(KeyboardInterrupt):
        sample.collect_review(
            output, questions, {"corpus_revision": "a" * 40}, session(provider, source)
        )
    record = json.loads(output.read_text())
    assert record["status"] == "failed"
    assert len(record["answers"]) == 32 and len(provider.questions) == 3
    assert [row["status"] for row in record["answers"][:3]] == [
        "recorded",
        "recorded",
        "interrupted",
    ]
    assert all(row["status"] == "not_started" for row in record["answers"][3:])


def test_setup_failure_keeps_the_exclusive_claim(tmp_path, questions):
    output = tmp_path / "sample.json"

    def failed_session():
        raise RuntimeError("private setup detail")

    with pytest.raises(RuntimeError):
        sample.collect_review(output, questions, {"corpus_revision": "a" * 40}, failed_session)
    record = json.loads(output.read_text())
    assert record["status"] == "failed"
    assert all(row["status"] == "not_started" for row in record["answers"])
    assert "private setup detail" not in output.read_text()


def test_wrong_model_fails_before_any_generation(tmp_path, questions, source):
    output = tmp_path / "sample.json"
    provider = Provider(output)
    provider.model = "other-model"
    with pytest.raises(ValueError, match="pinned"):
        sample.collect_review(
            output, questions, {"corpus_revision": "a" * 40}, session(provider, source)
        )
    assert not provider.calibration_calls and not provider.questions


def test_wrong_cohort_fails_before_claiming_a_run(tmp_path, questions, source):
    output = tmp_path / "sample.json"
    provider = Provider(output)
    with pytest.raises(ValueError, match="exact original"):
        sample.collect_review(output, list(reversed(questions)), {}, session(provider, source))
    assert not output.exists()


def test_export_all_questions_with_exact_sources_and_empty_judgements(tmp_path, questions, source):
    record, _, _ = collect(tmp_path, questions, source)
    before = copy.deepcopy(record)
    csv_path, md_path = tmp_path / "review.csv", tmp_path / "review.md"
    assert export.export_review(record, csv_path, md_path) == 32
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 32 and all(row[export.JUDGEMENT] == "" for row in rows)
    assert rows[0]["question"] == questions[0]["question"]
    assert questions[0]["key_facts"][0] in rows[0]["required_facts"]
    assert source.text in rows[0]["provided_sources"]
    assert source.chunk_id in rows[0]["provided_sources"]
    assert "/blob/" + "a" * 40 + "/docs/PAYOUTS.md#rule" in rows[0]["provided_sources"]
    assert '<a href="https://github.com/yy31104/ledgerops/blob/' in md_path.read_text()
    assert record == before


@pytest.mark.parametrize(
    "text", ["=1+1", "+SUM(A1:A2)", "-1+1", "@SUM(A1)", " \t=1+1", "\ttext", "\ntext"]
)
def test_formula_text_is_literal_in_csv(text):
    assert export.csv_text(text) == "'" + text


def test_export_escapes_model_html_and_formula_text(tmp_path, questions, source):
    record, _, _ = collect(tmp_path, questions, source)
    record["answers"][0]["question"] = '=HYPERLINK("https://example.invalid")'
    record["answers"][0]["result"]["text"] = "<script>unexpected()</script>"
    csv_path, md_path = tmp_path / "review.csv", tmp_path / "review.md"
    export.export_review(record, csv_path, md_path)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        first = next(csv.DictReader(stream))
    assert first["question"].startswith("'=")
    assert "<script>" not in md_path.read_text() and "&lt;script&gt;" in md_path.read_text()


def test_export_failure_does_not_replace_existing_files(tmp_path, questions, source):
    record, _, _ = collect(tmp_path, questions, source)
    csv_path, md_path = tmp_path / "review.csv", tmp_path / "review.md"
    md_path.write_text("Human notes already exist")
    with pytest.raises(FileExistsError):
        export.export_review(record, csv_path, md_path)
    assert not csv_path.exists() and md_path.read_text() == "Human notes already exist"


def test_export_rejects_existing_human_judgements(tmp_path, questions, source):
    record, _, _ = collect(tmp_path, questions, source)
    record["answers"][0]["judgement"] = "incomplete"
    with pytest.raises(ValueError, match="already contains"):
        export.export_review(record, tmp_path / "review.csv")
    assert not (tmp_path / "review.csv").exists()


def test_partial_record_exports_missing_answers_without_filling_verdicts(
    tmp_path, questions, source
):
    record, _, _ = collect(tmp_path, questions, source, fail=2)
    csv_path = tmp_path / "review.csv"
    export.export_review(record, csv_path)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["answer"] == "" and rows[1]["run_status"] == "error: TimeoutError"
    assert all(row[export.JUDGEMENT] == "" for row in rows)


@pytest.mark.parametrize("method", ["vector", "hybrid"])
def test_session_uses_selected_retrieval_and_vector_scope_gate(monkeypatch, corpus, method):
    from types import SimpleNamespace

    calls = []

    @contextmanager
    def database():
        yield "owned-test-database"

    def provider(*, model):
        calls.append(model)
        return SimpleNamespace(model=model)

    def postgres(dsn, key, embedder, *, method):
        assert dsn == "owned-test-database" and key == "key"
        return SimpleNamespace(method=method, name=method)

    monkeypatch.setattr(sample, "SubscriptionProvider", provider)
    monkeypatch.setattr(sample, "BgeSmallEmbedder", lambda: "pinned-test-encoder")
    monkeypatch.setattr(sample, "temporary_database", database)
    monkeypatch.setattr(sample, "setup", lambda dsn: None)
    monkeypatch.setattr(sample, "index_corpus", lambda *args: "key")
    monkeypatch.setattr(sample, "PostgresRetriever", postgres)
    with sample.subscription_session(corpus, method, 0.61) as (actual, retriever, gate):
        assert actual.model == sample.MODEL and gate.threshold == 0.61
        assert gate.retriever.method == "vector"
        base = retriever.retriever
        if method == "vector":
            assert base.method == "vector"
        else:
            assert [item.method for item in base.retrievers] == ["fts", "vector"]
    assert calls == [sample.MODEL]


@pytest.mark.parametrize("method", ["vector", "hybrid"])
def test_main_binds_current_policy_and_frozen_development_report(
    tmp_path, monkeypatch, corpus, method
):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"method": method, "scope_threshold": 0.61}))
    report = {
        "corpus": {"digest": corpus.digest},
        "scope_gate": {"threshold": 0.61},
        "context": {"retrieval_method": method},
        "environment": {"quality_signal": True, "backend": "postgres", "embedder": "test"},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    monkeypatch.setattr(sample, "POLICY", policy_path)
    monkeypatch.setattr(sample, "default_retrieval", lambda: method)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review",
            "--development-report",
            str(report_path),
            "--output",
            str(tmp_path / "raw.json"),
        ],
    )
    calls = []
    monkeypatch.setattr(
        sample,
        "collect_review",
        lambda output, questions, metadata, factory: calls.append(metadata),
    )
    sample.main()
    assert len(calls) == 1
    assert calls[0]["retrieval_method"] == method
    assert calls[0]["retrieval_policy"]["scope_threshold"] == 0.61
    assert calls[0]["questions_file"] == "eval/v4-dev/questions.jsonl"
    assert calls[0]["development_report_sha256"] == sample.sha(report_path)
    assert calls[0]["retrieval_policy_sha256"] == sample.sha(policy_path)


@pytest.mark.parametrize("problem", ["method", "threshold", "corpus", "standin"])
def test_main_refuses_mismatched_inputs_before_model_setup(tmp_path, monkeypatch, corpus, problem):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"method": "vector", "scope_threshold": 0.61}))
    report = {
        "corpus": {"digest": "wrong" if problem == "corpus" else corpus.digest},
        "scope_gate": {"threshold": 0.62 if problem == "threshold" else 0.61},
        "context": {"retrieval_method": "hybrid" if problem == "method" else "vector"},
        "environment": {
            "quality_signal": problem != "standin",
            "backend": "postgres",
            "embedder": "test",
        },
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    output = tmp_path / "raw.json"
    monkeypatch.setattr(sample, "POLICY", policy_path)
    monkeypatch.setattr(sample, "default_retrieval", lambda: "vector")
    monkeypatch.setattr(
        sys, "argv", ["review", "--development-report", str(report_path), "--output", str(output)]
    )

    def forbidden(*args):
        raise AssertionError("model setup must not run")

    monkeypatch.setattr(sample, "subscription_session", forbidden)
    with pytest.raises(ValueError):
        sample.main()
    assert not output.exists()
