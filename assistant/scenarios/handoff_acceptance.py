"""Actual operator API accepts the handoff; the assistant never submits it."""

import json
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from ledgerops_assistant.agent import Agent
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.drafts import setup
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.tools import ReadOnlyAPI
from ledgerops_assistant.workflow import DraftWorkflow
from tests.integration.conftest import seeded_db as seeded_db
from tests.integration.payout_helpers import money
from tests.integration.test_http_operator_actions import operator_service as operator_service
from tests.integration.test_http_operator_actions import propose_body

pytestmark = pytest.mark.integration


@pytest.fixture
def assistant_dsn(db):
    name = "assistant_" + uuid4().hex
    with db.connect("admin") as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = psycopg.conninfo.make_conninfo(
        host="127.0.0.1", port=db.port, user="postgres", password=db.admin_password, dbname=name
    )
    try:
        setup(dsn)
        yield dsn
    finally:
        with db.connect("admin") as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_handoff_to_real_console_proposal_does_not_approve_or_execute(
    operator_service, assistant_dsn
):
    story, service, maker, _ = operator_service
    intent = {
        key: value
        for key, value in propose_body(story).items()
        if key not in ("action_id", "action_type")
    }
    api = ReadOnlyAPI(f"http://127.0.0.1:{service.port}", maker)
    workflow = DraftWorkflow(assistant_dsn, Agent(BM25Retriever(load_corpus()), FakeProvider()))
    thread = str(uuid4())
    paused = workflow.invoke(
        thread,
        api,
        question="Inspect original terminal evidence before proposing.",
        request_id=str(uuid4()),
        intent=intent,
    )
    assert paused["status"] == "awaiting_review", paused.get("reason")
    handed = workflow.invoke(
        thread, api, decision={"continue": True, "draft_sha256": paused["draft"]["draft_sha256"]}
    )
    assert handed["status"] == "handoff_ready"
    payload = handed["draft"]["payload"]
    path = "/v1/operator/actions/" + payload["action_id"]
    assert service.request("GET", path, token=maker)[0] == 404
    # This test driver represents the human console submission, outside the assistant.
    status, raw, _ = service.request("POST", "/v1/operator/actions", payload, token=maker)
    assert status == 201
    proposal = json.loads(raw)
    assert proposal["review"]["gross_minor"] == "62"
    assert proposal["proposal_sha256"] != handed["draft"]["draft_sha256"]
    assert service.request("POST", "/v1/operator/actions", payload, token=maker)[:2] == (200, raw)
    assert money(story["database"], story["golden"])[:3] == (80, 62, 18)
    assert maker not in json.dumps(handed)
