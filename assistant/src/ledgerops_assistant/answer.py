"""One question in, one checked answer out.

    question
      -> routing.classify: action? live state? (no model involved)
      -> retriever.search: top passages
      -> ScopeGate: is the best passage close enough to be about this at all?
      -> provider.draft: answer text + cited passage IDs
      -> grounding check: keep only citations of passages actually given;
         no valid citation means no answer

Every refusal names its reason, so an evaluation can tell "refused correctly" from
"refused because retrieval missed".
"""

from dataclasses import asdict, dataclass, field

from ledgerops_assistant import routing
from ledgerops_assistant.context import retrieve_passages
from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import Provider
from ledgerops_assistant.retrieval import Retriever

LIVE_STATE_TEXT = (
    "The docs describe rules, not the current state of a payout or account. Read it from "
    "the API: GET /v1/payouts/{id} for a payout (payout:read), GET /v1/accounts for your "
    "balances (accounts:read), GET /v1/investigation/operations/{id} for an operation's "
    "entries and evidence (evidence:read)."
)
ACTION_TEXT = (
    "I can't do that: this assistant has read-only access and never approves, executes, "
    "releases or resubmits anything. Corrections to settled evidence go through the "
    "two-person operator approval workflow."
)
NOT_IN_DOCS_TEXT = "The LedgerOps docs don't cover this."
UNGROUNDED_TEXT = "I couldn't find a passage that supports an answer."

# Fixed references for refusals that do not depend on retrieval.
LIVE_STATE_SECTION = "docs/HTTP-SERVICE.md#api"
ACTION_SECTION = "docs/OPERATOR-ACTIONS.md#controlled-reprocessing-of-terminal-evidence"


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    section_id: str
    revision: str


@dataclass
class Answer:
    question: str
    route: str
    text: str
    refused: bool
    reason: str  # none | action_not_permitted | needs_live_state | not_in_docs | ungrounded
    citations: list[Citation] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)  # chunk IDs given to the provider
    dropped_citations: list[str] = field(default_factory=list)
    top_score: float | None = None
    provider: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ScopeGate:
    """Refuse when even the best passage is far from the question.

    The score comes from one retriever whose scores mean the same thing for every
    question (cosine similarity for the vector retriever). The threshold is chosen on
    the dev split of the evaluation set and checked on the test split.
    """

    def __init__(self, retriever: Retriever, threshold: float):
        self.retriever, self.threshold = retriever, threshold

    def top_score(self, question: str) -> float:
        hits = self.retriever.search(question, 1)
        return hits[0].score if hits else 0.0

    def admits(self, question: str) -> tuple[bool, float]:
        score = self.top_score(question)
        return score >= self.threshold, score


def _citation(chunk: Chunk, revision: str) -> Citation:
    return Citation(chunk.chunk_id, chunk.section_id, revision)


def answer(
    question: str,
    retriever: Retriever,
    provider: Provider,
    *,
    gate: ScopeGate | None = None,
    top_n: int = 5,
    revision: str = "",
) -> Answer:
    route = routing.classify(question)
    if route == routing.ACTION:
        return Answer(question, route, ACTION_TEXT, True, "action_not_permitted")
    if route == routing.LIVE_STATE:
        return Answer(question, route, LIVE_STATE_TEXT, True, "needs_live_state")

    passages = retrieve_passages(retriever, question, top_n)
    retrieved = [chunk.chunk_id for chunk in passages]
    top_score = None
    if gate is not None:
        admitted, top_score = gate.admits(question)
        if not admitted:
            return Answer(
                question,
                routing.OUT_OF_SCOPE,
                NOT_IN_DOCS_TEXT,
                True,
                "not_in_docs",
                retrieved=retrieved,
                top_score=top_score,
            )
    if not passages:
        return Answer(question, routing.OUT_OF_SCOPE, NOT_IN_DOCS_TEXT, True, "not_in_docs")

    draft = provider.draft(question, passages)
    given = {chunk.chunk_id: chunk for chunk in passages}
    kept = [c for c in dict.fromkeys(draft.citations) if c in given]
    dropped = [c for c in draft.citations if c not in given]
    citations = [_citation(given[c], revision) for c in kept]
    base = dict(
        retrieved=retrieved,
        dropped_citations=dropped,
        top_score=top_score,
        provider=provider.name,
        usage=draft.usage,
    )
    if draft.refused:
        return Answer(question, route, draft.answer, True, draft.refusal_reason, citations, **base)
    if not citations:
        return Answer(question, route, UNGROUNDED_TEXT, True, "ungrounded", **base)
    return Answer(question, route, draft.answer, False, "none", citations, **base)
