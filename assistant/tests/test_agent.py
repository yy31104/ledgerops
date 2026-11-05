import json
import threading
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest

from ledgerops_assistant.agent import ANSWER_PROMPT, Agent, Limits, OperatorContext
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.tools import ReadOnlyAPI

TOKEN = "lo1_" + "z" * 43
OBJECT = "40000000-0000-0000-0000-000000000003"
IDENTITY = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "actor_id": "00000000-0000-0000-0000-000000000002",
    "client_id": "00000000-0000-0000-0000-000000000003",
    "scopes": ["payout:read", "operations:read", "evidence:read", "accounts:read"],
}


def client(handler=None):
    return ReadOnlyAPI(
        "http://127.0.0.1:8080",
        TOKEN,
        transport=httpx.MockTransport(
            handler
            or (
                lambda r: httpx.Response(
                    200,
                    json=IDENTITY
                    if r.url.path == "/v1/me"
                    else {"knowledge": "UNKNOWN", "reservation_state": "HELD"},
                )
            )
        ),
    )


class ScriptedProvider:
    name = "openai:scripted"

    def __init__(self, on_generate=None):
        self.messages = []
        self.on_generate = on_generate

    def generate(self, instructions, message, schema, **kwargs):
        self.messages.append(message)
        if self.on_generate:
            self.on_generate()
        return json.dumps({"tool": "payout", "object_id": OBJECT}), {
            "input_tokens": 10,
            "output_tokens": 5,
        }


def test_fake_model_reads_and_cites_without_persisting_token(tiny_corpus):
    state = Agent(BM25Retriever(tiny_corpus), FakeProvider()).run(
        f"Is payout {OBJECT} paid right now?", client()
    )
    assert [o["tool"] for o in state["observations"]] == ["payout", "operation", "investigation"]
    assert state["result"]["citations"] and state["result"]["uncertainty"]
    assert state["steps"] <= 6 and state["cost_usd"] == 0
    assert TOKEN not in json.dumps(state)


def test_malicious_plan_cannot_call_write_tool(tiny_corpus):
    agent = Agent(
        BM25Retriever(tiny_corpus),
        FakeProvider(),
        planner=lambda s: {"tool": "approve", "object_id": OBJECT},
    )
    state = agent.run("Investigate this payout", client())
    assert state["result"]["refused"] and state["observations"] == []


def test_steps_cost_and_cancellation_stop_subsequent_calls(tiny_corpus):
    agent = Agent(
        BM25Retriever(tiny_corpus),
        FakeProvider(),
        limits=Limits(max_steps=2),
        planner=lambda s: {"tool": "payout", "object_id": OBJECT},
    )
    assert agent.run("Investigate payout", client())["result"]["reason"] == "step_limit"
    event = threading.Event()
    event.set()
    assert (
        agent.run("Investigate payout", client(), cancel=event)["result"]["reason"] == "cancelled"
    )
    agent = Agent(
        BM25Retriever(tiny_corpus),
        FakeProvider(),
        limits=Limits(max_cost_usd=0, input_usd_per_million=10),
    )
    state = agent.run("Investigate payout", client())
    assert state["observations"] == [] and state["result"]["refused"]


def test_cost_reservation_uses_a_conservative_utf8_byte_bound(tiny_corpus):
    limits = Limits(input_usd_per_million=2, output_usd_per_million=3)
    agent = Agent(BM25Retriever(tiny_corpus), FakeProvider(), limits=limits)
    message = "\u00e9" * 100
    input_bound = len(message.encode("utf-8")) + len(ANSWER_PROMPT.encode("utf-8")) + 4096
    expected = (input_bound * 2 + limits.max_output_tokens * 3) / 1e6
    assert agent._reserve({"cost_usd": 0}, message) == expected
    with pytest.raises(ValueError, match="cost_limit"):
        agent._reserve({"cost_usd": limits.max_cost_usd - expected / 2}, message)


def test_revocation_after_read_prevents_answer(tiny_corpus):
    revoked = False

    def handler(request):
        nonlocal revoked
        if request.url.path == "/v1/me":
            return httpx.Response(401) if revoked else httpx.Response(200, json=IDENTITY)
        revoked = True
        return httpx.Response(200, json={"knowledge": "UNKNOWN"})

    state = Agent(BM25Retriever(tiny_corpus), FakeProvider()).run(
        f"Is payout {OBJECT} paid?", client(handler)
    )
    assert state["result"]["reason"] == "authentication_required"
    assert state["steps"] == len(state["observations"]) == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        ("revoked", "authentication_required"),
        ("scope", "authentication_required"),
        ("workspace_id", "authentication_required"),
        ("actor_id", "authentication_required"),
        ("client_id", "authentication_required"),
        ("unavailable", "upstream_unavailable"),
    ],
)
def test_current_authorization_blocks_next_planning_disclosure(tiny_corpus, change, reason):
    read = False
    marker = "protected-planning-marker"
    requests = []

    def handler(request):
        nonlocal read
        requests.append(request.url.path)
        if request.url.path != "/v1/me":
            read = True
            return httpx.Response(200, json={"knowledge": marker})
        if not read:
            return httpx.Response(200, json=IDENTITY)
        if change == "revoked":
            return httpx.Response(401)
        if change == "unavailable":
            return httpx.Response(503)
        current = {**IDENTITY}
        if change == "scope":
            current["scopes"] = [s for s in IDENTITY["scopes"] if s != "payout:read"]
        else:
            current[change] = "00000000-0000-0000-0000-000000000004"
        return httpx.Response(200, json=current)

    provider, charges = ScriptedProvider(), []
    state = Agent(
        BM25Retriever(tiny_corpus),
        provider,
        limits=Limits(input_usd_per_million=1, output_usd_per_million=1),
    ).run("Investigate this payout", client(handler), on_charge=charges.append)

    assert state["observations"][0]["data"]["knowledge"] == marker
    assert state["result"]["reason"] == reason
    assert state["result"]["refused"] and state["steps"] == 1
    assert len(provider.messages) == len(charges) == 1
    assert all(marker not in message for message in provider.messages)
    assert charges[0] > 0 and state["cost_usd"] == charges[0]
    assert requests.count("/v1/payouts/" + OBJECT) == 1


@pytest.mark.parametrize("cancelled", [False, True])
def test_planner_finishing_after_deadline_cannot_start_read(tiny_corpus, monkeypatch, cancelled):
    now, requests, charges = 0, [], []
    cancel = threading.Event()
    monkeypatch.setattr("ledgerops_assistant.agent.time", SimpleNamespace(monotonic=lambda: now))

    def finish_planning():
        nonlocal now
        now = 61
        if cancelled:
            cancel.set()

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=IDENTITY)

    provider = ScriptedProvider(finish_planning)
    state = Agent(
        BM25Retriever(tiny_corpus),
        provider,
        limits=Limits(input_usd_per_million=1, output_usd_per_million=1),
    ).run("Investigate this payout", client(handler), cancel=cancel, on_charge=charges.append)

    assert state["result"]["reason"] == ("cancelled" if cancelled else "deadline_exceeded")
    assert state["observations"] == [] and set(requests) == {"/v1/me"}
    assert len(provider.messages) == len(charges) == state["steps"] == 1
    assert state["cost_usd"] == charges[0]


@pytest.mark.parametrize("node", ["_decide", "_answer"])
@pytest.mark.parametrize("reason", ["deadline_exceeded", "cancelled"])
def test_identity_lookup_stopping_run_cannot_reserve_or_infer(
    tiny_corpus, monkeypatch, node, reason
):
    now, requests, charges, reserves = 0, [], [], []
    cancel = threading.Event()
    monkeypatch.setattr("ledgerops_assistant.agent.time", SimpleNamespace(monotonic=lambda: now))

    def handler(request):
        nonlocal now
        requests.append(request.url.path)
        if len(requests) == 2:
            if reason == "deadline_exceeded":
                now = 61
            else:
                cancel.set()
        return httpx.Response(200, json=IDENTITY)

    api, provider = client(handler), ScriptedProvider()
    context = OperatorContext(api, api.identity().owner_key, cancel, 60, charges.append)
    agent = Agent(
        BM25Retriever(tiny_corpus),
        provider,
        limits=Limits(input_usd_per_million=1, output_usd_per_million=1),
    )
    monkeypatch.setattr(agent, "_reserve", lambda *args: reserves.append(args) or 0)
    state = {
        "question": "Inspect the payout evidence",
        "steps": 0,
        "cost_usd": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "observations": [],
        "documents": [asdict(tiny_corpus.chunks[0])],
    }

    result = getattr(agent, node)(state, SimpleNamespace(context=context))

    assert result["result"]["refused"] and result["result"]["reason"] == reason
    assert requests == ["/v1/me", "/v1/me"]
    assert provider.messages == charges == reserves == []


def test_billed_generation_requires_prices(tiny_corpus):
    paid = type("PaidProvider", (), {"name": "openai:example"})()
    with pytest.raises(ValueError, match="prices"):
        Agent(BM25Retriever(tiny_corpus), paid)
