import json
import threading

import httpx
import pytest

from ledgerops_assistant.agent import Agent, Limits
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


def test_billed_generation_requires_prices(tiny_corpus):
    paid = type("PaidProvider", (), {"name": "openai:example"})()
    with pytest.raises(ValueError, match="prices"):
        Agent(BM25Retriever(tiny_corpus), paid)
