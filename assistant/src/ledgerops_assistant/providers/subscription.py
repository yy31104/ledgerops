"""Opt-in, local-only ChatGPT-plan adapter using the official Codex Python SDK."""

import json
import os
import signal
import subprocess
import sys

from ledgerops_assistant.providers.base import parse_draft, user_message


class SubscriptionProvider:
    name = "chatgpt_subscription"

    def __init__(self, *, timeout: float = 60):
        if os.environ.get("CI") or os.environ.get("LEDGEROPS_ASSISTANT_SERVER"):
            raise ValueError("ChatGPT subscription is local development only")
        if os.environ.get("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION") != "1":
            raise ValueError("explicitly opt in with LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1")
        self.timeout = timeout

    def generate(
        self, instructions: str, message: str, schema: dict, *, timeout=None, max_output_tokens=512
    ) -> tuple[str, dict]:
        # Never read, copy or export auth.json. The official SDK owns authentication.
        env = {
            k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")
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
        try:
            output, _ = proc.communicate(
                json.dumps(
                    {
                        "instructions": instructions,
                        "message": message,
                        "schema": schema,
                    }
                ),
                timeout=min(self.timeout, timeout) if timeout else self.timeout,
            )
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise TimeoutError("local subscription request exceeded its deadline") from None
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
