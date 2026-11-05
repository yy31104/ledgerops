import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from ledgerops_assistant.agent import Agent
from ledgerops_assistant.drafts import DraftIntent, DraftStore, WorkflowConflict, setup
from ledgerops_assistant.providers.base import Draft
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.tools import AuthenticationRequired, ReadOnlyAPI
from ledgerops_assistant.workflow import DraftWorkflow, WorkflowContext

PAYOUT = "40000000-0000-0000-0000-000000000003"
OBSERVATION = "40000000-0000-0000-0000-000000000004"
RUN = "40000000-0000-0000-0000-000000000005"
TOKEN = "lo1_" + "q" * 43
OWNER = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "actor_id": "00000000-0000-0000-0000-000000000002",
    "client_id": "00000000-0000-0000-0000-000000000003",
    "scopes": ["payout:read", "operations:read", "evidence:read", "approval:propose"],
}
INTENT = {
    "payout_id": PAYOUT,
    "observation_id": OBSERVATION,
    "evidence_run_id": RUN,
    "reason": "Review the repaired local settlement against current evidence.",
    "ttl_seconds": 900,
}


def case():
    return {
        "payout": {"operation_id": PAYOUT, "knowledge": "SUCCEEDED", "version": "2"},
        "recent_observations": [{"observation_id": OBSERVATION, "state": "SUCCEEDED"}],
        "recent_incidents": [{"category": "LOCAL_BLOCKER", "observation_id": OBSERVATION}],
        "current_reconciliation_references": [{"run_id": RUN, "evidence_epoch": "7"}],
        "observations_have_more": False,
        "incidents_have_more": False,
        "reconciliations_have_more": False,
    }


def client(data, identity=None):
    def handle(request):
        assert request.method == "GET"
        if request.url.path == "/v1/me":
            return httpx.Response(200, json=identity or OWNER)
        if request.url.path.startswith("/v1/investigation/"):
            return httpx.Response(200, json=data)
        return httpx.Response(200, json=data["payout"])

    return ReadOnlyAPI("http://127.0.0.1:8080", TOKEN, transport=httpx.MockTransport(handle))


class CountingFake(FakeProvider):
    calls = 0

    def draft(self, *args):
        self.calls += 1
        return super().draft(*args)


def create(dsn, corpus):
    setup(dsn)
    model = CountingFake()
    return DraftWorkflow(dsn, Agent(BM25Retriever(corpus), model)), model


def begin(workflow, api):
    thread, request = str(uuid4()), str(uuid4())
    result = workflow.invoke(
        thread,
        api,
        question="Investigate before proposing reprocessing.",
        request_id=request,
        intent=INTENT,
    )
    return thread, request, result


@pytest.mark.postgres
def test_restart_resume_and_repeat_preserve_one_draft(pg_dsn, tiny_corpus):
    workflow, model = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, request, paused = begin(workflow, api)
    assert paused["status"] == "awaiting_review" and model.calls == 1
    restarted = DraftWorkflow(pg_dsn, workflow.agent)
    decision = {"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    rotated = client(case())
    rotated.token = "lo1_" + "r" * 43
    resumed = restarted.invoke(thread, rotated, decision=decision)
    assert resumed["status"] == "handoff_ready" and model.calls == 1
    assert restarted.invoke(thread, api, decision=decision)["draft"] == paused["draft"]
    with pytest.raises(WorkflowConflict):
        restarted.invoke(thread, api, decision={**decision, "continue": False})
    assert TOKEN not in json.dumps(resumed)
    with psycopg.connect(pg_dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM assistant_workflow.drafts WHERE request_id=%s", (request,)
            ).fetchone()[0]
            == 1
        )
        blobs = conn.execute("SELECT blob FROM checkpoint_blobs WHERE blob IS NOT NULL").fetchall()
        assert all(TOKEN.encode() not in bytes(row[0]) for row in blobs)


@pytest.mark.postgres
def test_changed_intent_revocation_other_actor_and_changed_evidence(pg_dsn, tiny_corpus):
    workflow, _ = create(pg_dsn, tiny_corpus)
    data = case()
    api = client(data)
    thread, request, paused = begin(workflow, api)
    with pytest.raises(WorkflowConflict):
        workflow.invoke(
            thread,
            api,
            question="Investigate before proposing reprocessing.",
            request_id=request,
            intent={**INTENT, "reason": "different intent"},
        )
    decision = {"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    with pytest.raises(AuthenticationRequired):
        workflow.invoke(thread, client(data, {**OWNER, "scopes": []}), decision=decision)
    with pytest.raises(WorkflowConflict):
        workflow.invoke(
            thread, client(data, {**OWNER, "actor_id": str(uuid4())}), decision=decision
        )
    data["payout"]["version"] = "3"
    with pytest.raises(WorkflowConflict, match="evidence changed"):
        workflow.invoke(thread, api, decision=decision)


@pytest.mark.postgres
def test_concurrent_node_replay_cannot_create_another_action(pg_dsn):
    setup(pg_dsn)
    store = DraftStore(pg_dsn)
    request = str(uuid4())

    def write(_):
        return store.get_or_create("owner", request, DraftIntent.parse(INTENT), [])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(write, [1, 2]))
    assert first == second
    with pytest.raises(WorkflowConflict):
        store.get_or_create(
            "owner", request, DraftIntent.parse({**INTENT, "reason": "changed"}), []
        )


@pytest.mark.postgres
def test_crash_gap_after_prepare_resumes_to_same_draft(pg_dsn, tiny_corpus):
    workflow, model = create(pg_dsn, tiny_corpus)
    api = client(case())
    identity = api.identity()
    thread, request_id = str(uuid4()), str(uuid4())
    key = identity.owner_key + "/" + thread
    initial = {"question": "Inspect a terminal blocker", "request_id": request_id, "intent": INTENT}
    config = {"configurable": {"thread_id": key}}
    with PostgresSaver.from_conn_string(pg_dsn) as saver:
        graph = workflow._graph(saver)
        prepared = graph.invoke(
            initial,
            config,
            context=WorkflowContext(api, identity.owner_key),
            interrupt_before=["review"],
            durability="sync",
        )
    recovered = workflow.invoke(thread, api, **initial)
    assert recovered["status"] == "awaiting_review"
    assert recovered["draft"] == prepared["draft"] and model.calls == 1


@pytest.mark.postgres
def test_unknown_observation_does_not_offer_a_draft(pg_dsn, tiny_corpus):
    workflow, _ = create(pg_dsn, tiny_corpus)
    data = case()
    data["payout"]["knowledge"] = "UNKNOWN"
    data["recent_observations"] = []
    _, _, result = begin(workflow, client(data))
    assert result["status"] == "refused" and "draft" not in result


@pytest.mark.postgres
def test_expired_draft_is_not_regenerated(pg_dsn, tiny_corpus):
    workflow, model = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, request, paused = begin(workflow, api)
    with psycopg.connect(pg_dsn) as conn:
        conn.execute(
            "UPDATE assistant_workflow.drafts SET expires_at=now()-interval '1 second' "
            "WHERE request_id=%s",
            (request,),
        )
    with pytest.raises(WorkflowConflict, match="expired"):
        workflow.invoke(
            thread,
            api,
            decision={"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]},
        )
    assert model.calls == 1


@pytest.mark.parametrize(
    "extra", [{"workspace_id": str(uuid4())}, {"action_type": "APPROVE"}, {"ttl_seconds": True}]
)
def test_draft_input_cannot_choose_identity_or_write_capability(extra):
    with pytest.raises((ValueError, TypeError)):
        DraftIntent.parse({**INTENT, **extra})


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("reason", "valid"),
    [
        ("x" * 256, True),
        ("é" * 128, True),
        (" " + "x" * 254 + " ", True),
        ("x" * 257, False),
        ("é" * 129, False),
        (" " + "x" * 255 + " ", False),
        ("x\0y", False),
        ("", False),
        (" \t\n", False),
        ("\ufeff", False),
        ("\u001c", False),
        ("\u0085", False),
        (" \ufeff\t\u001c", False),
    ],
)
def test_reason_contract_preserves_raw_reviewed_bytes(pg_dsn, tiny_corpus, reason, valid):
    workflow, model = create(pg_dsn, tiny_corpus)
    thread, request = str(uuid4()), str(uuid4())
    args = {
        "question": "Inspect the terminal blocker",
        "request_id": request,
        "intent": {**INTENT, "reason": reason},
    }
    if valid:
        paused = workflow.invoke(thread, client(case()), **args)
        assert paused["status"] == "awaiting_review"
        assert paused["draft"]["payload"]["reason"] == reason
        assert model.calls == 1
    else:
        with pytest.raises(ValueError, match="reason"):
            workflow.invoke(thread, client(case()), **args)
        assert model.calls == 0
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM assistant_workflow.drafts WHERE request_id=%s", (request,)
        ).fetchone()[0] == int(valid)


@pytest.mark.postgres
@pytest.mark.parametrize("mismatch", ["tool_object", "payout", "operation", "other_payout"])
def test_investigation_must_bind_the_requested_payout(pg_dsn, tiny_corpus, mismatch):
    workflow, _ = create(pg_dsn, tiny_corpus)
    data, other = case(), str(uuid4())
    if mismatch in ("tool_object", "other_payout"):

        def planner(state):
            decision = workflow.agent._fake_decision(state)
            if decision["tool"] == "investigation":
                decision["object_id"] = other
            return decision

        workflow.agent.planner = planner
        if mismatch == "other_payout":
            data["payout"]["operation_id"] = other
    elif mismatch == "payout":
        data["payout"]["operation_id"] = other
    else:
        data["operation"] = {"operation_id": other}
    _, _, result = begin(workflow, client(data))
    assert result["status"] == "refused"
    assert "draft" not in result


@pytest.mark.postgres
@pytest.mark.parametrize("failure", ["refusal", "model_error"])
def test_refused_investigation_cannot_prepare_a_draft(pg_dsn, tiny_corpus, monkeypatch, failure):
    workflow, model = create(pg_dsn, tiny_corpus)

    def fail(*args):
        model.calls += 1
        if failure == "model_error":
            raise TimeoutError("synthetic model failure after successful reads")
        return Draft("Evidence is insufficient.", [], True, "not_in_docs")

    monkeypatch.setattr(model, "draft", fail)
    _, _, result = begin(workflow, client(case()))
    assert result["status"] == "refused" and result["reason"] == "investigation_refused"
    assert any(o["tool"] == "investigation" for o in result["investigation"]["observations"])
    assert "draft" not in result and model.calls == 1


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payout", None),
        ("operation", []),
        ("recent_observations", None),
        ("recent_observations", [None]),
        ("recent_incidents", {}),
        ("current_reconciliation_references", ["invalid"]),
        ("observations_have_more", None),
        ("observations_have_more", ...),
        ("observations_have_more", 0),
        ("incidents_have_more", "false"),
        ("reconciliations_have_more", []),
    ],
)
def test_malformed_or_incomplete_projections_are_refused(pg_dsn, tiny_corpus, field, value):
    workflow, _ = create(pg_dsn, tiny_corpus)
    data = case()
    if value is ...:
        del data[field]
    else:
        data[field] = value
    _, _, result = begin(workflow, client(data))
    assert result["status"] == "refused" and "draft" not in result


@pytest.mark.postgres
@pytest.mark.parametrize(
    "incident",
    [
        {"category": "TERMINAL_CONTRADICTION", "observation_id": OBSERVATION},
        {"category": "LOCAL_BLOCKER", "observation_id": str(uuid4())},
    ],
)
def test_an_additional_incompatible_incident_prevents_a_draft(pg_dsn, tiny_corpus, incident):
    workflow, _ = create(pg_dsn, tiny_corpus)
    data = case()
    data["recent_incidents"].append(incident)
    _, _, result = begin(workflow, client(data))
    assert result["status"] == "refused" and "draft" not in result


@pytest.mark.postgres
def test_wrong_review_digest_does_not_consume_the_interrupt(pg_dsn, tiny_corpus):
    workflow, _ = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, _, paused = begin(workflow, api)
    with pytest.raises(WorkflowConflict, match="review decision"):
        workflow.invoke(thread, api, decision={"continue": True, "draft_sha256": "0" * 64})
    assert workflow.inspect(thread, api)["status"] == "awaiting_review"
    resumed = workflow.invoke(
        thread, api, decision={"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    )
    assert resumed["status"] == "handoff_ready" and resumed["draft"] == paused["draft"]


@pytest.mark.postgres
@pytest.mark.parametrize("field", ["workspace_id", "client_id"])
def test_workflow_and_draft_are_isolated_by_workspace_and_client(pg_dsn, tiny_corpus, field):
    workflow, _ = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, request, paused = begin(workflow, api)
    other = client(case(), {**OWNER, field: str(uuid4())})
    decision = {"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    with pytest.raises(WorkflowConflict, match="unavailable for this identity"):
        workflow.inspect(thread, other)
    with pytest.raises(WorkflowConflict, match="unavailable for this identity"):
        workflow.invoke(thread, other, decision=decision)
    independent = workflow.invoke(
        thread,
        other,
        question="Investigate before proposing reprocessing.",
        request_id=request,
        intent=INTENT,
    )
    assert independent["draft"]["action_id"] != paused["draft"]["action_id"]
    assert workflow.inspect(thread, api)["draft"] == paused["draft"]


@pytest.mark.postgres
def test_crash_after_draft_commit_replays_without_regenerating(pg_dsn, tiny_corpus, monkeypatch):
    workflow, model = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, request = str(uuid4()), str(uuid4())
    args = {"question": "Inspect the terminal blocker", "request_id": request, "intent": INTENT}
    persisted = []
    original = workflow.store.get_or_create

    def crash_after_commit(*args):
        persisted.append(original(*args))
        raise RuntimeError("synthetic crash after draft commit, before prepare checkpoint")

    monkeypatch.setattr(workflow.store, "get_or_create", crash_after_commit)
    with pytest.raises(RuntimeError, match="after draft commit"):
        workflow.invoke(thread, api, **args)
    assert len(persisted) == 1 and model.calls == 1
    restarted = DraftWorkflow(pg_dsn, workflow.agent)
    recovered = restarted.invoke(thread, api, **args)
    assert recovered["status"] == "awaiting_review" and recovered["draft"] == persisted[0]
    resumed = restarted.invoke(
        thread, api, decision={"continue": True, "draft_sha256": persisted[0]["draft_sha256"]}
    )
    assert resumed["status"] == "handoff_ready" and resumed["draft"] == persisted[0]
    assert model.calls == 1
    with psycopg.connect(pg_dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM assistant_workflow.drafts WHERE request_id=%s", (request,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.postgres
def test_concurrent_full_invocations_serialize_and_preserve_the_draft(
    pg_dsn, tiny_corpus, monkeypatch
):
    workflow, model = create(pg_dsn, tiny_corpus)
    api = client(case())
    thread, request = str(uuid4()), str(uuid4())
    args = {"question": "Inspect the terminal blocker", "request_id": request, "intent": INTENT}
    entered, release = threading.Event(), threading.Event()
    original = workflow.agent.run

    def blocked_investigation(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow.agent, "run", blocked_investigation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(workflow.invoke, thread, api, **args)
        try:
            assert entered.wait(5)
            other_process = DraftWorkflow(pg_dsn, workflow.agent)
            with pytest.raises(WorkflowConflict, match="already running"):
                other_process.invoke(thread, api, **args)
        finally:
            release.set()
        paused = first.result(timeout=10)
    repeated = other_process.invoke(thread, api, **args)
    assert repeated["draft"] == paused["draft"] and model.calls == 1
    resumed = other_process.invoke(
        thread, api, decision={"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    )
    assert resumed["status"] == "handoff_ready" and resumed["draft"] == paused["draft"]
    with psycopg.connect(pg_dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM assistant_workflow.drafts WHERE request_id=%s", (request,)
            ).fetchone()[0]
            == 1
        )
