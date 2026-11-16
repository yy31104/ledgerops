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
    r"rebuild|freeze|restore|export|rotate|update|modify|submit|transfer|reprocess|disburse"
)
_IMPERATIVE = re.compile(rf"^\s*((please|kindly)\s+)?({_VERBS})\b", re.IGNORECASE)
_REQUEST = re.compile(
    rf"\b(can|could|would|will)\s+you\s+((please|kindly)\s+)?"
    rf"((go\s+ahead\s+and\s+)|((help|assist)\s+(me|us)\s+(to\s+)?))?"
    rf"({_VERBS})\b",
    re.IGNORECASE,
)
_INDIRECT = re.compile(
    rf"\b((for\s+you\s+to|I\s+(need|want|would\s+like)\s+you\s+to)\s+"
    rf"((please|kindly)\s+)?({_VERBS})\b|"
    rf"(appreciate\s+it\s+if\s+you|it\s+would\s+help\s+if\s+you)\s+(could\s+)?"
    rf"((please|kindly)\s+)?({_VERBS})\b|"
    r"would\s+you\s+mind\s+((please|kindly)\s+)?"
    r"(releasing|approving|exporting|refunding|retrying|resubmitting|cancelling|settling)\b)",
    re.IGNORECASE,
)
_PASSIVE_REQUEST = re.compile(
    r"\b(can|could|would|will)\s+you\s+(please\s+)?(ensure|arrange|make\s+sure)\b"
    r".{0,100}\b(released|approved|sent|refunded|cancelled|settled|resubmitted)\b",
    re.IGNORECASE,
)
_EXPLANATORY_REQUEST = re.compile(
    r"^\s*(why|how|when|where)\s+(can|could|would|will)\s+you\b", re.IGNORECASE
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_SPECIFIC = re.compile(
    r"\b(my|our|this|that)\s+(payout|payment|hold|operation|account|balance|workspace|"
    r"transfer)\b|\bmerchant\s+[A-Z0-9][\w-]*'s\b",
    re.IGNORECASE,
)
_CURRENT = re.compile(
    r"\b(right now|currently|current|still|yet|today|at the moment|was .* paid|"
    r"has .* been|is .* paid|balances?|status)\b",
    re.IGNORECASE,
)
_READ_REQUEST = re.compile(
    r"^\s*(please\s+)?(check|look\s+up|show|get)\b|"
    r"\b(can|could|would|will)\s+you\s+(please\s+)?(check|look\s+up|show|get)\b",
    re.IGNORECASE,
)
# "What does X mean" or "why ... X" asks for a rule even when it names one object.
_RULE_QUESTION = re.compile(r"\b(mean|means|why|what should|should i|how do|how does)\b", re.I)
_STATE_QUESTION = re.compile(r"^\s*(was|is|has|did|what is|what's)\b", re.IGNORECASE)
_OVERRIDE = re.compile(
    r"ignore\s+((all|any|the)\s+)?"
    r"(previous|prior|earlier|system|developer|safety|operator)\b"
    r".{0,40}\b(rules|instructions|messages)\b|"
    r"\b(admin(istrator)?|system|developer|root)\s+(override|message|instructions)\b|"
    r"<\s*/?\s*(system|developer)\s*>|\[\s*(system|developer)\s*\]",
    re.IGNORECASE,
)
_SECRET_REQUEST = re.compile(r"\b(reveal|send|print|show)\b.*\b(token|secret|password)\b", re.I)
_MERCHANT_ID = re.compile(r"(?i:\bmerchant)\s+[A-Z][\w-]*\b")

# Explicit capability limits, rather than inferring support from a high similarity
# to a payment-related paragraph. This is a bounded product perimeter, not a
# general detector for every possible unsupported financial question.
_EXTERNAL_CAPABILITY = re.compile(
    r"\b(foreign\s+exchange|forex|exchange\s+rates?|currency\s+conversion|"
    r"card\s+(networks?|authori[sz]ation|acquirers?|interchange|fees?)|credit[-\s]+cards?|"
    r"cardholders?|"
    r"visa|mastercard|amex|chargebacks?|swift|sepa|ach|iban|"
    r"taxes|taxation|tax\s+(rates?|returns?|withholding|reporting)|vat|"
    r"withholding\s+tax|sanctions|kyc|aml|pci(?:[-\s]+dss)?|"
    r"bank(?:ing)?\s+(fees?|holidays?|cutoffs?|cut-offs?|routing))\b",
    re.IGNORECASE,
)


def unsupported_topic(question: str) -> bool:
    """Known external capabilities that the operator documentation does not provide."""
    return bool(_EXTERNAL_CAPABILITY.search(question))


def classify(question: str) -> str:
    # A cheap input guard, not the permission boundary. Tools enforce that separately.
    if _SECRET_REQUEST.search(question) or _OVERRIDE.search(question):
        return ACTION
    if _IMPERATIVE.search(question) or _INDIRECT.search(question):
        return ACTION
    if not _EXPLANATORY_REQUEST.search(question) and (
        _REQUEST.search(question) or _PASSIVE_REQUEST.search(question)
    ):
        return ACTION
    names_object = bool(
        _UUID.search(question) or _SPECIFIC.search(question) or _MERCHANT_ID.search(question)
    )
    asks_state = bool(
        _STATE_QUESTION.search(question)
        or _CURRENT.search(question)
        or _READ_REQUEST.search(question)
    )
    if names_object and asks_state and not _RULE_QUESTION.search(question):
        return LIVE_STATE
    return DOCS
