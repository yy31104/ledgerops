"""Explicit API model, strict structured output and no automatic inference retries."""

import os

from ledgerops_assistant.providers.base import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    parse_draft,
    user_message,
)


class OpenAIProvider:
    def __init__(self, model=None, client=None):
        self.model = model or os.environ.get("LEDGEROPS_ASSISTANT_OPENAI_MODEL")
        if not self.model:
            raise ValueError("set LEDGEROPS_ASSISTANT_OPENAI_MODEL explicitly")
        if client is None:
            from openai import OpenAI

            client = OpenAI(max_retries=0, timeout=30)
        self.client = client
        self.name = f"openai:{self.model}"

    def generate(self, instructions, message, schema, *, timeout=30, max_output_tokens=512):
        client = (
            self.client.with_options(timeout=timeout)
            if hasattr(self.client, "with_options")
            else self.client
        )
        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=message,
            max_output_tokens=max_output_tokens,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "operator_answer",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        usage = getattr(response, "usage", None)
        return response.output_text, {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

    def draft(self, question, passages):
        raw, usage = self.generate(SYSTEM_PROMPT, user_message(question, passages), ANSWER_SCHEMA)
        return parse_draft(raw, usage)
