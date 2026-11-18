import json
import threading
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from test_agent import IDENTITY, OBJECT, client

from ledgerops_assistant.agent import Agent, Limits, OperatorContext
from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.support import PassageSupport
from ledgerops_assistant.tools import AuthenticationRequired

QUESTION = "How do I export a snapshot?"


def chunk(name, body, heading="Operating instructions"):
    return Chunk(
        f"docs/EXAMPLE.md#{name}/0",
        f"docs/EXAMPLE.md#{name}",
        "docs/EXAMPLE.md",
        "Example",
        heading,
        f"{heading}\n\n{body}",
    )


def gate_for(tmp_path, *passages, cutoff=0.75):
    other = chunk("other", "The backup directory is local.")
    corpus = Corpus(tmp_path, "test", (*passages, other), {})
    coverage = DocumentCoverage(corpus, 0.597)
    return ScopeGate(
        None, None, coverage=coverage, support=PassageSupport(coverage), support_threshold=cutoff
    )


@pytest.mark.parametrize(
    "body",
    [
        "Export a run report. Restore a snapshot.",
        "Export a run report.\n\nRestore a snapshot.",
        "- Export a run report\n- Restore a snapshot",
        "| Export | Run report |\n| Restore | Snapshot |",
        "Example command:\n```sh\nexport snapshot\n```",
        "The screen lists snapshot metadata.",
    ],
)
def test_matches_never_join_separate_evidence_or_take_a_title_as_a_witness(tmp_path, body):
    source = chunk("only", body, heading="Export a snapshot")
    other = chunk("target", "To export a snapshot, open its manifest and run the export job.")
    gate = gate_for(tmp_path, source, other)
    missing = gate.support.measure(QUESTION, [source])
    assert missing.score == 0 and missing.witness is None
    supported = gate.support.measure(QUESTION, [source, other])
    assert supported.score == pytest.approx(1.0)
    assert supported.chunk_id == other.chunk_id
    assert supported.witness in other.body


@pytest.mark.parametrize("question", ["How do you export a snapshot?", "How to export a snapshot?"])
def test_equivalent_procedure_wording_still_requires_local_evidence(tmp_path, question):
    split = chunk("split", "Export a run report. Restore a snapshot.")
    joined = chunk("joined", "To export a snapshot, run the export job.")
    gate = gate_for(tmp_path, split, joined, cutoff=0.1)
    assert not gate.supports(question, [split])
    assert gate.supports(question, [joined])


@pytest.mark.parametrize(
    "body",
    [
        "You cannot export a snapshot.",
        "Snapshot export is not supported.",
        "The service does not offer snapshot export.",
    ],
)
def test_denial_answers_a_capability_question_but_supplies_no_procedure(tmp_path, body):
    source = chunk("denial", body)
    gate = gate_for(tmp_path, source)
    assert gate.supports("Can I export a snapshot?", [source])
    assert not gate.supports(QUESTION, [source])


def test_unrelated_negative_clause_keeps_the_supported_action(tmp_path):
    source = chunk(
        "positive", "A suspended account cannot authenticate, but users can export a snapshot."
    )
    assert gate_for(tmp_path, source).supports(QUESTION, [source])


def test_contrast_clause_must_independently_support_the_procedure(tmp_path):
    denied = "You cannot export a snapshot while maintenance is active"
    supported = "you can export a snapshot after maintenance ends."
    source = chunk("supported", f"{denied}, but {supported}")
    unrelated = chunk("unrelated", f"{denied}, but you can export a run report.")
    gate = gate_for(tmp_path, source, unrelated)

    result = gate.support.measure(QUESTION, [source])
    assert result.witness == supported and gate.supports(QUESTION, [source])
    assert gate.support.measure(QUESTION, [unrelated]).witness is None


class ObservedFake(FakeProvider):
    def __init__(self):
        self.calls = 0

    def draft(self, question, passages):
        self.calls += 1
        return super().draft(question, passages)


def retriever(*passages):
    return SimpleNamespace(context=lambda _query, _k: list(passages))


def test_standalone_answer_checks_selected_passages_before_model(tmp_path):
    unrelated = chunk("other", "Export a run report. Restore a snapshot.")
    source = chunk("evidence", "To export a snapshot, run the export job.")
    gate = gate_for(tmp_path, unrelated, source)
    provider = ObservedFake()
    declined = answer(QUESTION, retriever(unrelated), provider, gate=gate)
    assert declined.refused and declined.reason == "not_in_docs"
    assert provider.calls == 0
    supported = answer(QUESTION, retriever(source), provider, gate=gate)
    assert not supported.refused and provider.calls == 1


def test_authenticated_planner_requires_evidence_after_fitting(tmp_path):
    filler = chunk("filler", "Report metadata. " * 65)
    source = chunk("evidence", "To export a snapshot, run the export job. " * 12)
    gate = gate_for(tmp_path, filler, source)
    provider = ObservedFake()
    attempts = []
    requests = []

    def authenticated(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=IDENTITY)

    def planner(_state):
        attempts.append("planned")
        return {"tool": "finish", "object_id": None}

    def run(limit):
        return Agent(
            retriever(filler, source),
            provider,
            planner=planner,
            scope_gate=gate,
            limits=Limits(max_input_bytes=limit),
        ).run(QUESTION, client(authenticated))

    assert gate.supports(QUESTION, [filler, source])
    stopped = run(6500)
    assert stopped["result"]["reason"] == "not_in_docs"
    assert stopped["steps"] == provider.calls == 0 and attempts == []
    assert set(requests) == {"/v1/me"}

    supported = run(24000)
    assert not supported["result"]["refused"]
    assert attempts == ["planned"] and provider.calls == 1


def test_growing_observations_cannot_replace_fitted_document_evidence(tmp_path):
    unrelated = chunk("filler", "Report metadata. " * 65)
    source = chunk("evidence", "To export a snapshot, run the export job. " * 12)
    gate = gate_for(tmp_path, unrelated, source)
    provider = ObservedFake()
    cap = Limits(max_input_bytes=7000)
    agent = Agent(retriever(unrelated, source), provider, scope_gate=gate, limits=cap)
    api = client()
    runtime = SimpleNamespace(
        context=OperatorContext(api, api.identity().owner_key, threading.Event(), float("inf"))
    )
    state = {
        "route": "docs",
        "question": QUESTION,
        "steps": 0,
        "cost_usd": 0,
        "documents": [asdict(unrelated), asdict(source)],
        "observations": [],
    }
    # The same budget can answer before a read. A large API observation forces
    # the fitter to drop the only document witness. Its text is not a doc witness.
    assert not agent._answer(state, runtime)["result"]["refused"]
    assert provider.calls == 1
    state["observations"] = [
        {
            "tool": "operation",
            "endpoint": "/v1/operations/example",
            "source_id": "api:operation",
            "status": "ok",
            "observed_at": "2026-11-18T00:00:00Z",
            "warning": "Live API observation",
            "data": {"note": "To export a snapshot, run the export job. " * 55},
        }
    ]
    stopped = agent._answer(state, runtime)
    assert stopped["result"]["reason"] == "not_in_docs"
    assert provider.calls == 1


def test_live_state_and_action_routes_keep_their_existing_boundary(tmp_path):
    source = chunk("evidence", "To export a snapshot, run the export job.")
    provider = ObservedFake()
    agent = Agent(retriever(), provider, scope_gate=gate_for(tmp_path, source))
    live = agent.run(f"Is payout {OBJECT} paid right now?", client())
    assert not live["result"]["refused"] and live["observations"]
    assert provider.calls == 1
    action = agent.run(f"Approve payout {OBJECT} now.", client())
    assert action["result"]["reason"] == "action_not_permitted"
    assert action["observations"] == [] and provider.calls == 1


def test_unauthenticated_request_never_fetches_documents(tmp_path):
    def inaccessible(_query, _k):
        pytest.fail("document lookup before authentication")

    source = chunk("evidence", "Run the export job to export a snapshot.")
    agent = Agent(
        SimpleNamespace(context=inaccessible),
        ObservedFake(),
        scope_gate=gate_for(tmp_path, source),
    )
    with pytest.raises(AuthenticationRequired):
        agent.run(QUESTION, client(lambda _request: httpx.Response(401)))


def test_cli_uses_configured_support_cutoff(tmp_path, monkeypatch, capsys):
    from ledgerops_assistant import __main__ as cli
    from ledgerops_assistant import policy

    source = chunk("unsupported", "Export the event log. Restore a snapshot.")
    other = chunk("other", "The backup directory is local.")
    corpus = Corpus(tmp_path, "test", (source, other), {})
    monkeypatch.setattr(cli, "load_corpus", lambda: corpus)
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    configuration = {"coverage_threshold": 0.597, "support_threshold": 0.75}
    monkeypatch.setattr(policy, "_policy", lambda: configuration)
    argv = [QUESTION, "--backend", "memory", "--embedder", "hashing", "--gate", "-1", "--json"]

    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "not_in_docs"

    configuration.pop("support_threshold")
    assert cli.main(argv) == 0
    assert not json.loads(capsys.readouterr().out)["refused"]
