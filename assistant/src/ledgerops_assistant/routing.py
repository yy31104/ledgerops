"""Decide whether the docs can answer a question at all, before any model sees it.

Three kinds of question are not documentation questions:

- requests to act ("release the hold", "can you approve ..."): the assistant has no
  write access, so it refuses whatever the model would say;
- questions about the current state of one specific object ("was payout X paid?"):
  the docs describe rules, not facts, so the answer has to come from the live API;
- questions the docs do not cover: detected after retrieval, from how weak the best
  match is (see ``answer.ScopeGate``).

These are deliberately simple rules. A rule that misfires is visible and testable; a
model asked to police itself is neither.
"""

import re

ACTION = "action"
LIVE_STATE = "live_state"
DOCS = "docs"
OUT_OF_SCOPE = "out_of_scope"

_VERBS = (
    r"release|approve|execute|refund|resubmit|retry|resend|cancel|post|delete|unfreeze|"
    r"resume|close|reverse|pay|send|reopen|enable|disable|revoke|issue|settle|mark|force|"
    r"rebuild|freeze|restore"
)
_IMPERATIVE = re.compile(rf"^\s*(please\s+)?({_VERBS})\b", re.IGNORECASE)
_REQUEST = re.compile(
    rf"\b(can|could|would|will)\s+you\s+(please\s+)?(go\s+ahead\s+and\s+)?({_VERBS})\b",
    re.IGNORECASE,
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_SPECIFIC = re.compile(
    r"\b(my|our|this|that)\s+(payout|payment|hold|operation|account|balance|workspace|"
    r"transfer)\b|\bmerchant\s+[A-Z0-9][\w-]*'s\b",
    re.IGNORECASE,
)
_CURRENT = re.compile(
    r"\b(right now|currently|current|still|yet|today|at the moment|was .* paid|"
    r"has .* been|is .* paid|balance)\b",
    re.IGNORECASE,
)
# "What does X mean" or "why ... X" asks for a rule even when it names one object.
_RULE_QUESTION = re.compile(r"\b(mean|means|why|what should|should i|how do|how does)\b", re.I)
_STATE_QUESTION = re.compile(r"^\s*(was|is|has|did|what is|what's)\b", re.IGNORECASE)
_OVERRIDE = re.compile(
    r"ignore .*?(rules|instructions)|ignore all earlier|system override|developer message|<system>",
    re.IGNORECASE,
)
_SECRET_REQUEST = re.compile(r"\b(reveal|send|print|show)\b.*\b(token|secret|password)\b", re.I)


def classify(question: str) -> str:
    # A cheap input guard, not the permission boundary. Tools enforce that separately.
    if _SECRET_REQUEST.search(question) or (
        _OVERRIDE.search(question) and re.search(rf"\b({_VERBS})\b", question, re.I)
    ):
        return ACTION
    if _IMPERATIVE.search(question) or _REQUEST.search(question):
        return ACTION
    names_object = bool(_UUID.search(question) or _SPECIFIC.search(question))
    asks_state = bool(_STATE_QUESTION.search(question) or _CURRENT.search(question))
    if names_object and asks_state and not _RULE_QUESTION.search(question):
        return LIVE_STATE
    return DOCS
