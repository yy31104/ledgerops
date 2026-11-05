"""Structured generation through a configured Ollama server (loopback by default)."""

import json
import os
import urllib.request

from ledgerops_assistant.providers.base import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    parse_draft,
    user_message,
)


class OllamaProvider:
    def __init__(self, model=None, url=None, timeout=30):
        self.model = model or os.environ.get("LEDGEROPS_ASSISTANT_OLLAMA_MODEL")
        if not self.model:
            raise ValueError("set LEDGEROPS_ASSISTANT_OLLAMA_MODEL explicitly")
        self.url = url or os.environ.get("LEDGEROPS_ASSISTANT_OLLAMA_URL", "http://127.0.0.1:11434")
        self.timeout = timeout
        self.name = f"ollama:{self.model}"

    def generate(self, instructions, message, schema, *, timeout=30, max_output_tokens=512):
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_predict": max_output_tokens},
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": message},
            ],
        }
        request = urllib.request.Request(
            self.url.rstrip("/") + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=min(timeout, self.timeout)) as response:
            reply = json.loads(response.read())
        return reply["message"]["content"], {
            "input_tokens": reply.get("prompt_eval_count"),
            "output_tokens": reply.get("eval_count"),
        }

    def draft(self, question, passages):
        raw, usage = self.generate(SYSTEM_PROMPT, user_message(question, passages), ANSWER_SCHEMA)
        return parse_draft(raw, usage)
