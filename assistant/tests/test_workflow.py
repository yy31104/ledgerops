import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psycopg
import pytest

from ledgerops_assistant.agent import Agent
from ledgerops_assistant.drafts import DraftIntent, DraftStore, WorkflowConflict, setup
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.tools import AuthenticationRequired, ReadOnlyAPI
from ledgerops_assistant.workflow import DraftWorkflow

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
    resumed = restarted.invoke(thread, api, decision=decision)
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
