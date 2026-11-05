"""A deterministic stand-in for a language model, used in CI and tests.

It copies the two sentences from the passages that share the most words with the
question and cites the passages they came from. It costs nothing and never changes
between runs, so tests can assert on exact output. It cannot paraphrase, combine facts
or notice that a passage is off-topic, so its answers say nothing about answer quality.
"""

import json
import re

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import Draft
from ledgerops_assistant.retrieval import tokenize


class FakeProvider:
    name = "fake"

    def draft(self, question: str, passages: list[Chunk]) -> Draft:
        # Scripted live summaries improve the fake demo, not measured model quality.
        for chunk in passages:
            if not chunk.chunk_id.startswith("api:"):
                continue
            observation = json.loads(chunk.body)
            if observation.get("tool") == "payout" and observation.get("status") == "ok":
                data = observation["data"]
                if "knowledge" in data:
                    text = f"Current API observation: {data['knowledge']}."
                    if "reservation_state" in data:
                        text += f" Reservation: {data['reservation_state']}."
                    if "gross_minor" in data:
                        text += f" Reserved gross amount: {data['gross_minor']} minor units."
                    text += (
                        " This is a caller-scoped observation at the stated time. "
                        "Recent lists are bounded."
                    )
                    return Draft(text, [chunk.chunk_id])
        wanted = set(tokenize(question))
        candidates = []
        for rank, chunk in enumerate(passages):
            for sentence in re.split(r"(?<=[.!?])\s+", chunk.body.replace("\n", " ")):
                overlap = len(wanted & set(tokenize(sentence)))
                if overlap and not sentence.lstrip().startswith(("```", "|")):
                    candidates.append((-overlap, rank, sentence.strip(), chunk.chunk_id))
        if not candidates:
            return Draft("The passages do not answer this question.", [], True, "not_in_docs")
        best = sorted(candidates)[:2]
        best.sort(key=lambda item: item[1])  # keep passage order for readability
        answer = " ".join(sentence for _, _, sentence, _ in best)
        citations = list(dict.fromkeys(chunk_id for *_, chunk_id in best))
        return Draft(answer, citations)
