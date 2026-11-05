import json
from types import SimpleNamespace

import pytest

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers import get_provider
from ledgerops_assistant.providers.base import ANSWER_SCHEMA, SYSTEM_PROMPT
from ledgerops_assistant.providers.ollama import OllamaProvider
from ledgerops_assistant.providers.openai_api import OpenAIProvider

PASSAGE = Chunk("d.md#a/0", "d.md#a", "d.md", "D", "A", "D > A\n\nA timeout keeps the hold.")
REPLY = {
    "answer": "It keeps the hold.",
    "citations": ["d.md#a/0"],
    "refused": False,
    "refusal_reason": "none",
}


def test_openai_request_uses_strict_schema_and_reports_usage():
    seen = {}

    class Responses:
        def create(self, **kwargs):
            seen.update(kwargs)
            usage = SimpleNamespace(input_tokens=120, output_tokens=30)
            return SimpleNamespace(output_text=json.dumps(REPLY), usage=usage)

    provider = OpenAIProvider(model="test-model", client=SimpleNamespace(responses=Responses()))
    draft = provider.draft("Does a timeout release it?", [PASSAGE])
    assert draft.citations == ["d.md#a/0"] and draft.usage["input_tokens"] == 120
    assert seen["model"] == "test-model" and seen["instructions"] == SYSTEM_PROMPT
    assert seen["text"]["format"]["strict"] is True
    assert seen["text"]["format"]["schema"] == ANSWER_SCHEMA
    assert "[d.md#a/0]" in seen["input"]


def test_openai_requires_an_explicit_model(monkeypatch):
    monkeypatch.delenv("LEDGEROPS_ASSISTANT_OPENAI_MODEL", raising=False)
    with pytest.raises(ValueError):
        OpenAIProvider(client=object())


def test_ollama_sends_schema_and_zero_temperature(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps(REPLY)}, "eval_count": 9}).encode()

    def fake_urlopen(request, timeout):
        seen["url"], seen["body"] = request.full_url, json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    draft = OllamaProvider(model="local", url="http://127.0.0.1:11434").draft("q", [PASSAGE])
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert seen["body"]["format"] == ANSWER_SCHEMA and seen["body"]["options"]["temperature"] == 0
    assert draft.citations == ["d.md#a/0"] and draft.usage["output_tokens"] == 9


def test_subscription_provider_requires_explicit_local_opt_in(monkeypatch):
    monkeypatch.delenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", raising=False)
    with pytest.raises(ValueError, match="opt in|local development"):
        get_provider("chatgpt_subscription")


def test_subscription_is_rejected_in_ci_even_with_opt_in(monkeypatch):
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(ValueError, match="local development"):
        get_provider("chatgpt_subscription")
