"""Fake-model LangGraph against the actual authenticated HTTP process."""

import json

import pytest

from ledgerops.db.payout_dispatch import dispatch_once
from ledgerops_assistant.agent import Agent
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.tools import ReadOnlyAPI
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.http_helpers import ProcessService
from tests.integration.payout_helpers import (
    NOW,
    PAYOUT_ID,
    admit,
    fund_merchant,
    money,
    payout_input,
)
from tests.integration.payout_observation_helpers import trust
from tests.integration.test_authentication import provision

pytestmark = pytest.mark.integration


def test_real_timeout_stays_held_after_agent_investigation(db, golden, tmp_path):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    fund_merchant(db, golden)
    principal, command, _ = payout_input(golden)
    assert admit(db, golden).http_status == 202

    class Provider:
        def put(self, *args):
            raise TimeoutError("synthetic")

        def lookup(self, *args):
            raise TimeoutError("synthetic")

        def close(self, *args):
            raise TimeoutError("synthetic")

    with db.connect("worker") as conn:
        result = dispatch_once(
            conn,
            command.workspace_id,
            command.operation_id,
            Provider(),
            trust=trust(command),
            clock=lambda: NOW,
        )
        assert result.outcome == "TIMEOUT"
    token = provision(db, principal, scopes=["payout:read", "operations:read"]).token
    process = ProcessService(db, tmp_path / "service")
    try:
        api = ReadOnlyAPI(f"http://127.0.0.1:{process.port}", token)
        state = Agent(BM25Retriever(load_corpus()), FakeProvider()).run(
            f"Is payout {PAYOUT_ID} paid right now?", api
        )
        payout = state["observations"][0]
        assert payout["status"] == "ok" and "UNKNOWN" in json.dumps(payout["data"])
        assert "HELD" in json.dumps(payout["data"])
        assert state["observations"][2]["status"] == "forbidden"
        assert token not in json.dumps(state)
        assert tuple(money(db, golden)[:3]) == (80, 62, 18)
    finally:
        process.close()
