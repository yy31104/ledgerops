"""Decide whether the docs can answer a question at all, before any model sees it.

Three kinds of question are not documentation questions:

- requests to act ("release the hold", "can you approve ..."): the assistant has no
  write access, so it refuses whatever the model would say;
- questions about the current state of one specific object ("was payout X paid?"):
  the docs describe rules, not facts, so the answer has to come from the live API;
- questions the docs do not cover: a few named external capabilities below, then
  the question's terms against the indexed corpus and the strength of the best match
  (see ``answer.ScopeGate`` and ``coverage``).

Assistant-directed requests use a closed grammar: explanation and read requests
are permitted; other directives fail closed, including unfamiliar operation verbs.
This is an input policy, not the permission boundary or a universal injection detector.
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
_IMPERATIVE = re.compile(rf"(?:^|[.!?;]\s+)\s*((please|kindly)\s+)?({_VERBS})\b", re.IGNORECASE)
# Capture the requested behavior, independently of any list of mutation verbs.
_DIRECTED_REQUEST = re.compile(
    r"\bwould\s+you\s+mind\s+(?P<mind>.+)|"
    r"\b(?:can|could|would|will|may|might)\s+you\s+(?:please\s+|kindly\s+)?(?P<body>.+)|"
    r"\bfor\s+you\s+to\s+(?P<possible>.+)|"
    r"\b(?:I|we)\s+(?:need|want|would\s+like|ask)\s+you\s+to\s+(?P<need>.+)|"
    r"\b(?:appreciate\s+it\s+if\s+you|it\s+would\s+help\s+if\s+you)\s+"
    r"(?:could\s+)?(?P<appreciate>.+)|"
    r"^\s*(?:please|kindly)\s+(?P<polite>.+)",
    re.IGNORECASE,
)
_EXPLANATORY_REQUEST = re.compile(
    r"^\s*(why|how|when|where)\s+(can|could|would|will)\s+you\b", re.IGNORECASE
)
_EXPLANATION = re.compile(
    r"^\s*(?:please\s+|kindly\s+)?(?:"
    r"explain(?:ing)?|describ(?:e|ing)|summari[sz](?:e|ing)|clarify(?:ing)?|"
    r"compar(?:e|ing)|defin(?:e|ing)|outlin(?:e|ing)|teach(?:ing)?|"
    r"walk(?:ing)?\s+(?:me|us)\s+through|help(?:ing)?\s+(?:me|us)\s+understand|"
    r"tell(?:ing)?\s+(?:me|us)\s+(?:why|how|what|when|whether|which)|"
    r"show(?:ing)?\s+(?:me|us)\s+how)\b",
    re.IGNORECASE,
)
_READ_INTENT = re.compile(
    r"^\s*(?:please\s+|kindly\s+)?(?:"
    r"look(?:ing)?\s+up|inspect(?:ing)?|investigat(?:e|ing)|read(?:ing)?|"
    r"show(?:ing)?|list(?:ing)?|fetch(?:ing)?|get(?:ting)?|check(?:ing)?|"
    r"retriev(?:e|ing)|find(?:ing)?|tell(?:ing)?\s+(?:me|us))\s+"
    r"(?:(?:me|us|the|a|an|my|our|this|that|these|those|all|current|latest|recent|"
    r"available|pending|original|terminal|authorized|saved|persisted|selected|bounded|whether)\s+)*"
    r"(?P<object>payouts?|payments?|operations?|investigations?|accounts?|balances?|metrics|"
    r"status|evidence|documentation|docs|guides?|runbooks?|logs?|"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_INVESTIGATION = re.compile(r"^\s*(?:inspect(?:ing)?|investigat(?:e|ing))\b", re.IGNORECASE)
_FOLLOWUP = re.compile(
    r"\b(?:and\s+then|and\s+also|then|after\s+that)\s+|[.!?;]\s+(?:also\s+)?", re.IGNORECASE
)
_MUTATING_FOLLOWUP = re.compile(rf"\band\s+(?:please\s+)?(?:{_VERBS})\b", re.IGNORECASE)
_QUESTION = re.compile(
    r"(?:^|[.!?;]\s+|,\s+)\s*(?:what|why|when|where|which|who|how|"
    r"can|could|should|would|will|does|"
    r"do(?!\s+(?:whatever|anything|everything|something|it|this|that|as|what)\b)|"
    r"did|is|are|was|were|has|have|may|must)\b",
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
_ROLE_DIRECTIVE = re.compile(
    r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+"
    r"(?:(?:a|an|the)\s+)?(?:system|developer|admin(?:istrator)?|root)\b|"
    r"\b(?:system|developer|admin(?:istrator)?|root)\s*[:>]\s*|"
    r"\btreat\b.{0,70}\b(?:trusted|authori[sz]ed|system|developer)\b|"
    r"(?:^|[.!?;]\s+)\s*(?:disregard|bypass|override|forget)\b"
    r".{0,70}\b(?:rules|instructions|policy|safety|guardrails)\b",
    re.IGNORECASE,
)
_SECRET_REQUEST = re.compile(
    r"\b(reveal|send|print|show)\b(?!\s+(?:me|us)\s+how\b).*\b(token|secret|password)\b", re.I
)
_MERCHANT_ID = re.compile(r"(?i:\bmerchant)\s+[A-Z][\w-]*\b")

# Explicit capability limits, rather than inferring support from a high similarity
# to a payment-related paragraph. This is a bounded product perimeter, not a
# general detector for every possible unsupported financial question.
_EXTERNAL_CAPABILITY = re.compile(
    r"\b(foreign\s+exchange|forex|exchange\s+rates?|currency\s+conversion|"
    r"cards?|interchange|merchant\s+category|mcc|credit\s+cards?|"
    r"cardholders?|"
    r"visa|mastercard|amex|chargebacks?|swift|sepa|ach|iban|psd2|sca|blik|nbp|"
    r"tax(?:es|ation)?|vat|"
    r"withholding\s+tax|sanctions|kyc|aml|pci(?:[-\s]+dss)?|"
    r"bank(?:ing)?\s+(fees?|holidays?|cutoffs?|cut-offs?|routing))\b",
    re.IGNORECASE,
)


def unsupported_topic(question: str) -> bool:
    """Known external capabilities that the operator documentation does not provide."""
    # Hyphenated financial terms carry the same meaning as their spaced form.
    normalized = re.sub(r"[-_\u2010-\u2015]+", " ", question)
    return bool(_EXTERNAL_CAPABILITY.search(normalized))


def _permitted_request(body: str) -> bool:
    """Permit only explicit explanation/read intent, including follow-up requests."""
    if _MUTATING_FOLLOWUP.search(body):
        return False
    return all(
        _EXPLANATION.search(part) or _READ_INTENT.search(part) or _INVESTIGATION.search(part)
        for part in _FOLLOWUP.split(body)
    )


def classify(question: str) -> str:
    # A cheap input guard, not the permission boundary. Tools enforce that separately.
    if (
        _SECRET_REQUEST.search(question)
        or _OVERRIDE.search(question)
        or _ROLE_DIRECTIVE.search(question)
    ):
        return ACTION
    if _IMPERATIVE.search(question):
        return ACTION
    directed = _DIRECTED_REQUEST.search(question)
    body = (
        next(value for value in directed.groupdict().values() if value is not None)
        if directed
        else question
    )
    if directed and not _EXPLANATORY_REQUEST.search(question):
        if not _permitted_request(body):
            return ACTION
    if not directed and not (_QUESTION.search(question) or _permitted_request(question)):
        # Bare directives with unfamiliar verbs are not silently treated as docs.
        return ACTION
    if _EXPLANATION.search(body) or _EXPLANATORY_REQUEST.search(question):
        return DOCS
    read = _READ_INTENT.search(body)
    if read:
        return (
            DOCS
            if read["object"].lower()
            in {"documentation", "docs", "guide", "guides", "runbook", "runbooks"}
            else LIVE_STATE
        )
    if _INVESTIGATION.search(body):
        return LIVE_STATE
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
