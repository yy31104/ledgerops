import json
import sys
from types import SimpleNamespace

from ledgerops_assistant.providers.base import ANSWER_SCHEMA
from ledgerops_assistant.providers.subscription import SubscriptionProvider


def test_adapter_passes_schema_but_not_api_key_to_worker(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    seen = {}

    class Process:
        returncode = 0

        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        def communicate(self, raw, timeout):
            seen["request"] = json.loads(raw)
            return json.dumps({"text": "{}", "usage": {"billing": "chatgpt_plan"}}), ""

    monkeypatch.setattr("subprocess.Popen", Process)
    assert (
        SubscriptionProvider().generate("instructions", "evidence", ANSWER_SCHEMA)[1]["billing"]
        == "chatgpt_plan"
    )
    assert "OPENAI_API_KEY" not in seen["env"]
    assert seen["request"]["schema"] == ANSWER_SCHEMA


def test_sdk_worker_is_ephemeral_and_denies_escalation(monkeypatch, capsys):
    import io

    from ledgerops_assistant.providers import subscription_worker

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    seen = {}

    class Session:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def account(self):
            return SimpleNamespace(account=SimpleNamespace(type="chatgpt"))

        def thread_start(self, **kwargs):
            seen.update(kwargs)
            return self

        def run(self, *args, **kwargs):
            seen["run"] = kwargs
            return SimpleNamespace(status="completed", usage=None, final_response="{}")

    module = SimpleNamespace(
        Codex=Session,
        CodexConfig=lambda **kwargs: kwargs,
        ApprovalMode=SimpleNamespace(deny_all="never"),
        ExternalMessage=lambda **kwargs: kwargs,
        Sandbox=SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", module)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"instructions": "i", "message": "q", "schema": ANSWER_SCHEMA})),
    )
    subscription_worker.main()
    assert seen["ephemeral"] and seen["approval_mode"] == "never"
    assert seen["sandbox"] == "read-only" and seen["run"]["output_schema"] == ANSWER_SCHEMA
    assert json.loads(capsys.readouterr().out)["usage"]["billing"] == "chatgpt_plan"
