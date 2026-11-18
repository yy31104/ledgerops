import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_api import HEADERS, TOKEN
from test_workflow import INTENT, OWNER, PAYOUT

from ledgerops_assistant.agent import MAX_INPUT_BYTES, Agent, Limits
from ledgerops_assistant.answer import ScopeGate
from ledgerops_assistant.api import create_app
from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.drafts import setup
from ledgerops_assistant.providers.base import Draft
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.settings import Settings
from ledgerops_assistant.support_semantic import (
    CONTEXT_SECTIONS,
    SUPPORT_PROMPT,
    SUPPORT_SCHEMA,
    SubscriptionContextRetriever,
    SubscriptionDocumentCoverage,
    SubscriptionPassageSupport,
)
from ledgerops_assistant.telemetry import Telemetry
from ledgerops_assistant.tools import ReadOnlyAPI

QUESTION = "Does a timeout release the hold?"
QUOTE = "A timeout never releases the hold."
USAGE = {"input_tokens": 120, "output_tokens": 20}


@pytest.fixture
def verified_app(tiny_corpus):
    def build(*, status="supported", effect=None, corpus=tiny_corpus, dsn="", limits=None):
        context = ContextRetriever(BM25Retriever(corpus), corpus)
        verification, generation, upstream, lexical = [], [], [], []
        expired = threading.Event()

        def lexical_support(question, documents):
            lexical.append(question)
            return False

        context.scope_gate = SimpleNamespace(
            admits=lambda question: (True, None),
            supports=lexical_support,
        )

        def check(instructions, message, schema, *, timeout, cancel):
            assert instructions == SUPPORT_PROMPT and schema == SUPPORT_SCHEMA
            data = json.loads(message)
            verification.append((data, timeout))
            if effect is not None:
                effect(expired, cancel)
            if status == "error":
                raise RuntimeError("private provider diagnostic")
            supported = status != "unsupported"
            proof = {
                "supported": supported,
                "chunk_id": data["passages"][0]["chunk_id"] if supported else None,
                "quote": QUOTE if supported else None,
            }
            if status == "invalid":
                proof["quote"] = "A fabricated quote"
            return json.dumps(proof), USAGE

        def api_factory(token):
            def api(request):
                upstream.append((request.method, request.url.path))
                if request.url.path == "/v1/me":
                    return (
                        httpx.Response(401) if expired.is_set() else httpx.Response(200, json=OWNER)
                    )
                return httpx.Response(200, json={"knowledge": "UNKNOWN"})

            return ReadOnlyAPI("http://127.0.0.1:8090", token, transport=httpx.MockTransport(api))

        def generate(question, passages):
            generation.append((question, passages))
            return Draft(QUOTE, [passages[0].chunk_id])

        app = create_app(
            Settings(scope_verifier="subscription", database_url=dsn, limits=limits or Limits()),
            bind_host="127.0.0.1",
            retriever=context,
            api_factory=api_factory,
            telemetry=Telemetry(InMemorySpanExporter()),
            passage_retriever=SubscriptionContextRetriever(context.retriever, corpus),
            passage_verifier=SubscriptionPassageSupport(SimpleNamespace(generate=check)),
        )
        app.state.agents["fake"].provider = SimpleNamespace(name="fake", draft=generate)
        return app, context, verification, generation, upstream, lexical

    return build


def ask(client, question=QUESTION, request_id=None):
    return client.post(
        "/v1/assist",
        headers=HEADERS,
        json={"question": question, "request_id": request_id or str(uuid4())},
    )


def result(response):
    assert response.status_code == 200
    for item in response.text.split("\n\n"):
        if item.startswith("event: result\ndata: "):
            return json.loads(item.removeprefix("event: result\ndata: "))
    pytest.fail(response.text)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("supported", "none"),
        ("unsupported", "not_in_docs"),
        ("invalid", "not_in_docs"),
        ("error", "passage_verifier_unavailable"),
    ],
)
def test_http_verifies_once_without_planning_or_lexical_fallback(verified_app, status, reason):
    app, _, verification, generation, upstream, lexical = verified_app(status=status)
    with TestClient(app) as client:
        response = ask(client)
    payload = result(response)
    report = payload["verification"]
    assert payload["route"] == "docs"
    assert payload["answer"]["reason"] == reason
    assert report["status"] == status
    assert report["duration_ms"] >= 0
    assert report["usage"] == ({} if status == "error" else USAGE)
    assert payload["usage"]["input_tokens"] == 0
    assert len(verification) == 1 and not lexical
    data, timeout = verification[0]
    assert data["question"] == QUESTION and set(data) == {"question", "passages"}
    assert 0 < timeout <= 60 and "private provider diagnostic" not in response.text
    assert all(value not in json.dumps(data) for value in (TOKEN, OWNER["actor_id"]))
    assert set(upstream) == {("GET", "/v1/me")}
    assert '"phase": "decide"' not in response.text
    if status == "supported":
        question, passages = generation[0]
        assert question == QUESTION and len(generation) == 1
        assert data["passages"] == [{"chunk_id": c.chunk_id, "body": c.body} for c in passages]
        assert report["passage_ids"] == [c.chunk_id for c in passages]
        assert payload["steps"] == 1
    else:
        assert not generation and payload["steps"] == 0


@pytest.mark.parametrize("trim", [False, True])
def test_http_fits_the_same_pack_to_both_serialized_prompts(verified_app, tmp_path, trim):
    chunks = tuple(
        Chunk(
            f"docs/PAYOUTS.md#example-{i}/0",
            f"docs/PAYOUTS.md#example-{i}",
            "docs/PAYOUTS.md",
            "Payouts",
            f"Example {i}",
            f"Payouts > Example {i}\n\n{QUOTE}\n" + "evidence " * 490,
        )
        for i in range(12)
    )
    corpus = Corpus(tmp_path, "test", chunks, {})
    question = QUESTION + (" " + "é" * 7000 if trim else "")
    app, context, verification, generation, _, _ = verified_app(corpus=corpus)
    expected = SubscriptionContextRetriever(context.retriever, corpus).context(
        question, CONTEXT_SECTIONS
    )
    assert len(expected) == CONTEXT_SECTIONS
    with TestClient(app) as client:
        payload = result(ask(client, question))
    assert payload["answer"]["reason"] == "none"
    assert len(verification) == len(generation) == 1
    data, _ = verification[0]
    _, given = generation[0]
    assert given and (len(given) < len(expected) if trim else given == expected)
    assert given == expected[: len(given)]
    assert data["passages"] == [{"chunk_id": c.chunk_id, "body": c.body} for c in given]
    assert Agent._input_bytes(json.dumps(data), SUPPORT_PROMPT) <= MAX_INPUT_BYTES


def test_http_requires_authentication_and_rechecks_identity_after_verification(verified_app):
    app, _, verification, generation, _, _ = verified_app(effect=lambda expired, _: expired.set())
    with TestClient(app) as client:
        unauth = client.post("/v1/assist", json={"question": QUESTION, "request_id": str(uuid4())})
        assert unauth.status_code == 401 and not verification
        payload = result(ask(client))
    assert payload["answer"]["reason"] == "authentication_required"
    assert "verification" not in payload and len(verification) == 1 and not generation


def test_live_and_action_requests_bypass_subscription_verification(verified_app):
    app, _, verification, _, upstream, _ = verified_app()
    with TestClient(app) as client:
        live = result(ask(client, f"Is payout {PAYOUT} paid right now?"))
        action = result(ask(client, f"Approve payout {PAYOUT} now."))
        topic = result(ask(client, "How are chargebacks reconciled?"))
    assert live["route"] == "live_state" and not live["answer"]["refused"]
    assert ("GET", f"/v1/payouts/{PAYOUT}") in upstream
    assert action["route"] == "action" and action["answer"]["reason"] == "action_not_permitted"
    assert topic["answer"]["reason"] == "not_in_docs"
    assert all("verification" not in item for item in (live, action, topic))
    assert not verification


def test_http_semantic_coverage_only_aliases_documented_acronyms(verified_app, tmp_path):
    source = Chunk(
        "docs/PAYOUTS.md#recovery/0",
        "docs/PAYOUTS.md#recovery",
        "docs/PAYOUTS.md",
        "Payouts",
        "Recovery",
        f"Payouts > Recovery\n\n{QUOTE} The dispatch API uses FIFOs.",
    )
    corpus = Corpus(tmp_path, "test", (source,), {})
    app, _, verification, _, _, _ = verified_app(corpus=corpus)
    agent = app.state.agents["fake"]
    lexical = DocumentCoverage(corpus, 0.597)
    semantic = SubscriptionDocumentCoverage(corpus, 0.597)
    agent.scope_gate = ScopeGate(None, None, coverage=lexical)
    agent.passage_retriever.scope_gate = ScopeGate(None, None, coverage=semantic)

    question = "Does the FIFO release the hold on timeout?"
    assert lexical.measure(question).unknown_codes == ("FIFO",)
    assert semantic.measure(question).unknown_codes == ()
    assert semantic.measure("Do the APIs use FIFOs?").unknown_codes == ()
    assert semantic.measure("Do the APIS use FIFOs?").unknown_codes == ("APIS",)
    with TestClient(app) as client:
        valid = result(ask(client, question))
        unseen = result(ask(client, question.replace("FIFO", "ABC")))
    assert valid["verification"]["status"] == "supported"
    assert unseen["answer"]["reason"] == "not_in_docs" and "verification" not in unseen
    assert len(verification) == 1


@pytest.mark.postgres
def test_http_drafts_keep_the_existing_investigation_path(verified_app, pg_dsn):
    setup(pg_dsn)
    app, _, verification, generation, _, lexical = verified_app(dsn=pg_dsn)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/drafts/{uuid4()}",
            headers=HEADERS,
            json={
                "question": "Could you explain how payout recovery starts?",
                "request_id": str(uuid4()),
                "intent": INTENT,
            },
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "investigation_refused"
    assert lexical and not verification and not generation


def test_http_cancellation_during_verification_stops_generation(verified_app):
    started = threading.Event()

    def block(_, cancel):
        started.set()
        assert cancel.wait(5)

    app, _, verification, generation, _, _ = verified_app(effect=block)
    request_id = str(uuid4())
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ask, client, request_id=request_id)
        assert started.wait(5)
        assert client.delete(f"/v1/runs/{request_id}", headers=HEADERS).status_code == 200
        payload = result(pending.result(timeout=5))
    assert payload["answer"]["reason"] == "cancelled"
    assert len(verification) == 1 and not generation


def test_http_verification_uses_remaining_request_deadline(verified_app, monkeypatch):
    now = 0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr("ledgerops_assistant.agent.time", clock)
    monkeypatch.setattr("ledgerops_assistant.api.time", clock)

    def finish_after_deadline(_, cancel):
        nonlocal now
        now = 121

    app, _, verification, generation, _, _ = verified_app(
        effect=finish_after_deadline, limits=Limits(deadline_seconds=120)
    )
    with TestClient(app) as client:
        payload = result(ask(client))
    assert payload["answer"]["reason"] == "deadline_exceeded"
    assert len(verification) == 1 and verification[0][1] == 120
    assert not generation


@pytest.mark.parametrize("bind_host", [None, "0.0.0.0", "::", "localhost", "192.0.2.1"])
def test_subscription_scope_verifier_requires_loopback_ip(bind_host):
    with pytest.raises(ValueError, match="loopback IP bind"):
        create_app(Settings(scope_verifier="subscription"), bind_host=bind_host)


@pytest.mark.parametrize("guard", ["ALLOW_SUBSCRIPTION", "CI", "SERVER"])
def test_real_http_verifier_inherits_local_plan_guards(monkeypatch, guard):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("LEDGEROPS_ASSISTANT_SERVER", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_SCOPE_VERIFIER", "subscription")
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    if guard == "ALLOW_SUBSCRIPTION":
        monkeypatch.delenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION")
    else:
        monkeypatch.setenv("CI" if guard == "CI" else "LEDGEROPS_ASSISTANT_SERVER", "1")
    settings = Settings.environment()
    assert settings.limits.deadline_seconds == 120
    with pytest.raises(ValueError, match="opt in|local development only"):
        create_app(settings, bind_host="::1")
