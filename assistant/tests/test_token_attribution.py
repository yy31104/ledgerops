"""Token attribution design checks; the real calls run only locally."""

from collections import Counter

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.providers.base import SYSTEM_PROMPT
from scripts.token_attribution import fit, plan, request_text


def test_plan_compares_both_configurations_and_passage_counts():
    passages = [Chunk(f"d#a/{i}", "d#a", "d", "D", "a", f"text {i}") for i in range(11)]
    conditions = plan(passages)
    assert len(conditions) == 24 and conditions == plan(passages)
    counts = Counter((c["name"], c["isolated"]) for c in conditions)
    assert counts[("empty", False)] == counts[("full", False)] == 4
    assert counts[("empty", True)] == counts[("full", True)] == 4
    assert {c["passages"] for c in conditions if c["isolated"]} == {None, 0, 3, 6, 11}
    doubled = next(c for c in conditions if c["name"] == "double-instructions")
    instructions, message = request_text(doubled, "Why?", passages)
    assert instructions == SYSTEM_PROMPT + "\n\n" + SYSTEM_PROMPT and "[d#a/10]" in message


def test_least_squares_fit_reports_intercept_slope_and_residual():
    assert fit([(0, 10), (2, 12), (4, 14)]) == {
        "intercept": 10.0,
        "slope": 1.0,
        "max_abs_residual": 0.0,
    }
