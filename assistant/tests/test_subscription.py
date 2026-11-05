import json
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ledgerops_assistant.providers.base import ANSWER_SCHEMA
from ledgerops_assistant.providers.subscription import SubscriptionProvider


def test_adapter_passes_schema_but_not_api_key_to_worker(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
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
    assert "OPENAI_BASE_URL" not in seen["env"] and seen["start_new_session"] is True
    assert seen["request"]["schema"] == ANSWER_SCHEMA


@pytest.mark.parametrize("account_type", ["chatgpt", "apiKey"])
def test_sdk_worker_is_ephemeral_and_denies_escalation(monkeypatch, capsys, account_type):
    import io

    from ledgerops_assistant.providers import subscription_worker

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    seen = {}

    class Session:
        def __init__(self, config):
            seen["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def account(self):
            return SimpleNamespace(account=SimpleNamespace(type=account_type))

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
    if account_type != "chatgpt":
        with pytest.raises(ValueError, match="ChatGPT sign-in"):
            subscription_worker.main()
        assert "run" not in seen and "ephemeral" not in seen
        return
    subscription_worker.main()
    assert set(seen["config"]["config_overrides"]) == {
        'web_search="disabled"',
        "features.shell_tool=false",
        "features.unified_exec=false",
        "mcp_servers={}",
        "apps._default.enabled=false",
    }
    assert seen["ephemeral"] and seen["approval_mode"] == "never"
    assert seen["run"]["effort"] == "low"
    assert seen["sandbox"] == "read-only" and seen["run"]["output_schema"] == ANSWER_SCHEMA
    assert json.loads(capsys.readouterr().out)["usage"]["billing"] == "chatgpt_plan"


def test_subscription_deadline_kills_the_worker_process_group(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("LEDGEROPS_ASSISTANT_SERVER", raising=False)
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION", "1")
    killed, drained = [], []

    class Worker:
        pid = 424242

        def communicate(self, *args, **kwargs):
            drained.append(1)
            if len(drained) == 1:
                raise subprocess.TimeoutExpired("synthetic", kwargs["timeout"])
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Worker())
    monkeypatch.setattr("os.killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(TimeoutError, match="deadline"):
        SubscriptionProvider(timeout=0.1).generate("i", "q", ANSWER_SCHEMA)
    assert killed == [(424242, signal.SIGKILL)] and len(drained) == 2
