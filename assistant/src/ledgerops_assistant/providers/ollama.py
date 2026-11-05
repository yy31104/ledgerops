"""A local model served by Ollama on this machine: free and offline, usually weaker.

Uses Ollama's /api/chat endpoint with a JSON schema in ``format`` so the reply has the
same shape as every other provider's. Nothing leaves the machine.
"""

import json
import os
import urllib.request

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    Draft,
    parse_draft,
    user_message,
)


class OllamaProvider:
    def __init__(self, model: str | None = None, url: str | None = None, timeout: float = 120):
        self.model = model or os.environ.get("LEDGEROPS_ASSISTANT_OLLAMA_MODEL")
        if not self.model:
            raise ValueError("set LEDGEROPS_ASSISTANT_OLLAMA_MODEL to a model pulled into Ollama")
        self.url = url or os.environ.get("LEDGEROPS_ASSISTANT_OLLAMA_URL", "http://127.0.0.1:11434")
        self.timeout = timeout
        self.name = f"ollama:{self.model}"

    def draft(self, question: str, passages: list[Chunk]) -> Draft:
        body = {
            "model": self.model,
            "stream": False,
            "format": ANSWER_SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message(question, passages)},
            ],
        }
        request = urllib.request.Request(
            self.url.rstrip("/") + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            reply = json.loads(response.read())
        usage = {
            "input_tokens": reply.get("prompt_eval_count"),
            "output_tokens": reply.get("eval_count"),
        }
        return parse_draft(reply["message"]["content"], usage)
