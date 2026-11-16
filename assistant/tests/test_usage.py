import json

import pytest
from test_agent import client

from ledgerops_assistant.agent import Agent, Limits
from ledgerops_assistant.retrieval import BM25Retriever
from ledgerops_assistant.usage import accumulate


class ReportedProvider:
    name = "openai:scripted"

    def __init__(self, *, malformed=False, timeout=False, missing=False):
        self.calls = 0
        self.malformed, self.timeout, self.missing = malformed, timeout, missing

    def generate(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return (
                "broken" if self.malformed else '{"tool":"finish","object_id":null}',
                {"input_tokens": 100, "output_tokens": 20},
            )
        if self.timeout:
            raise TimeoutError("completion may have occurred upstream")
        return json.dumps(
            {
                "answer": "No supported evidence.",
                "citations": [],
                "refused": True,
                "refusal_reason": "not_in_docs",
            }
        ), {} if self.missing else {"input_tokens": 300, "output_tokens": 40}


def run(corpus, provider):
    reports = []
    state = Agent(
        BM25Retriever(corpus),
        provider,
        limits=Limits(input_usd_per_million=2, output_usd_per_million=4),
    ).run("Does a timeout release a hold?", client(), on_usage=reports.append)
    return state, reports


def test_planning_and_answer_usage_are_both_reported(tiny_corpus):
    state, reports = run(tiny_corpus, ReportedProvider())
    usage = state["usage"]
    assert usage["input_tokens"] == state["input_tokens"] == 400
    assert usage["output_tokens"] == state["output_tokens"] == 60
    assert usage["model_calls"] == usage["reported_model_calls"] == 2
    assert usage["complete"] and len(reports) == 2 and reports[-1] == usage
    assert usage["billing"] == "token_priced"
    assert usage["token_cost_estimate_usd"] == pytest.approx(0.00104)
    assert state["cost_usd"] > usage["token_cost_estimate_usd"]
    assert state["duration_ms"] > 0


def test_malformed_planning_still_retains_reported_usage(tiny_corpus):
    state, reports = run(tiny_corpus, ReportedProvider(malformed=True))
    assert state["result"]["reason"] == "model_or_budget_error"
    assert state["usage"]["input_tokens"] == 100
    assert state["usage"]["model_calls"] == len(reports) == 1
    assert state["usage"]["complete"]


@pytest.mark.parametrize("failure", ["timeout", "missing"])
def test_unknown_call_usage_is_not_reported_as_zero_cost(tiny_corpus, failure):
    state, reports = run(tiny_corpus, ReportedProvider(**{failure: True}))
    usage = state["usage"]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20
    assert not usage["complete"]
    assert usage["model_calls"] == 2 and usage["reported_model_calls"] == 1
    assert usage["token_cost_estimate_usd"] is None
    assert len(reports) == 2


def test_subscription_usage_is_quota_consumption_not_zero_dollar_billing(tiny_corpus):
    provider = ReportedProvider()
    provider.name = "chatgpt_subscription"
    state, _ = run(tiny_corpus, provider)
    assert state["usage"]["complete"] and state["usage"]["input_tokens"] == 400
    assert state["usage"]["billing"] == "subscription_quota"
    assert state["usage"]["token_cost_estimate_usd"] is None


@pytest.mark.parametrize("value", [None, -1, True, "100", 1.5])
def test_invalid_usage_is_incomplete_not_coerced(value):
    update = accumulate({}, {"input_tokens": value, "output_tokens": 3}, True)
    assert update["input_tokens"] == 0 and update["output_tokens"] == 3
    assert not update["usage_complete"]
