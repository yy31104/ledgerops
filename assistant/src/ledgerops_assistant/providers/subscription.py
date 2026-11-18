"""Opt-in, local-only ChatGPT-plan adapter using the official Codex Python SDK."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress

from ledgerops_assistant.providers.base import parse_draft, user_message

_WORKER_LEDGEROPS_ENV = {
    "LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION",
    "LEDGEROPS_ASSISTANT_SERVER",
}
_WORKER_EXCLUDED_ENV = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ASSISTANT_RUNTIME_PASSWORD",
    "DATABASE_URL",
}


def _kill_worker(proc):
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


class SubscriptionProvider:
    name = "chatgpt_subscription"

    def __init__(self, *, timeout: float = 60, model: str | None = None, isolated: bool = True):
        if os.environ.get("CI") or os.environ.get("LEDGEROPS_ASSISTANT_SERVER"):
            raise ValueError("ChatGPT subscription is local development only")
        if os.environ.get("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION") != "1":
            raise ValueError("explicitly opt in with LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1")
        self.timeout, self.isolated = timeout, isolated
        self.model = model or os.environ.get(
            "LEDGEROPS_ASSISTANT_SUBSCRIPTION_MODEL", "gpt-6.1-sol"
        )

    def generate(
        self,
        instructions: str,
        message: str,
        schema: dict,
        *,
        timeout=None,
        max_output_tokens=512,
        cancel=None,
    ) -> tuple[str, dict]:
        deadline = time.monotonic() + (
            min(self.timeout, timeout) if timeout is not None else self.timeout
        )
        if cancel is not None and cancel.is_set():
            raise TimeoutError("local subscription request was cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("local subscription request exceeded its deadline")

        # Never read, copy or export auth.json. The official SDK owns authentication.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _WORKER_LEDGEROPS_ENV
            or (
                key not in _WORKER_EXCLUDED_ENV
                and not key.startswith(("LEDGEROPS_", "PG", "POSTGRES_"))
            )
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "ledgerops_assistant.providers.subscription_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        done = threading.Event()

        def stop_on_cancel():
            while not done.wait(0.1):
                if cancel.is_set():
                    _kill_worker(proc)
                    return

        watcher = None
        if cancel is not None:
            watcher = threading.Thread(target=stop_on_cancel, daemon=True)
            watcher.start()
        try:
            output, _ = proc.communicate(
                json.dumps(
                    {
                        "instructions": instructions,
                        "message": message,
                        "schema": schema,
                        "model": self.model,
                        "isolated": self.isolated,
                    }
                ),
                timeout=max(0, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired:
            _kill_worker(proc)
            proc.communicate()
            raise TimeoutError("local subscription request exceeded its deadline") from None
        finally:
            done.set()
            if watcher is not None:
                watcher.join()
        if cancel is not None and cancel.is_set():
            raise TimeoutError("local subscription request was cancelled")
        if proc.returncode:
            raise RuntimeError(
                "local Codex request failed; check login and the optional SDK install"
            )
        result = json.loads(output)
        return result["text"], result["usage"]

    def draft(self, question, passages):
        from ledgerops_assistant.providers.base import ANSWER_SCHEMA, SYSTEM_PROMPT

        text, usage = self.generate(SYSTEM_PROMPT, user_message(question, passages), ANSWER_SCHEMA)
        return parse_draft(text, usage)
