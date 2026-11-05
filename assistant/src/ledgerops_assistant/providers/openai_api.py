"""OpenAI's API with an API key: billed per token, the default for evaluation runs.

Uses the Responses API with a strict JSON schema, so the reply always parses into the
same fields. The model name has no default on purpose: pick one explicitly with
LEDGEROPS_ASSISTANT_OPENAI_MODEL so a run records exactly what it measured.
Requires the optional dependency: ``uv sync --extra openai``.
"""

import os

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    Draft,
    parse_draft,
    user_message,
)


class OpenAIProvider:
    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get("LEDGEROPS_ASSISTANT_OPENAI_MODEL")
        if not self.model:
            raise ValueError("set LEDGEROPS_ASSISTANT_OPENAI_MODEL explicitly")
        if client is None:
            from openai import OpenAI  # optional dependency

            client = OpenAI()  # reads OPENAI_API_KEY from the environment
        self.client = client
        self.name = f"openai:{self.model}"

    def draft(self, question: str, passages: list[Chunk]) -> Draft:
        response = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=user_message(question, passages),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "operator_answer",
                    "schema": ANSWER_SCHEMA,
                    "strict": True,
                }
            },
        )
        usage = getattr(response, "usage", None)
        counts = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        return parse_draft(response.output_text, counts)
