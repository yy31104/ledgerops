import pytest

from ledgerops_assistant.routing import ACTION, DOCS, LIVE_STATE, classify


@pytest.mark.parametrize(
    "question, route",
    [
        ("Release the hold on payout 40000000-0000-4000-8000-000000000003.", ACTION),
        ("Please approve the pending action.", ACTION),
        ("Can you resubmit the payout?", ACTION),
        ("Could you please go ahead and release it?", ACTION),
        ("Can a timeout release the hold?", DOCS),
        ("Can I retry a refused payout with the same key?", DOCS),
        ("Was payout 40000000-0000-4000-8000-000000000003 paid?", LIVE_STATE),
        ("Is my payout still on hold?", LIVE_STATE),
        ("What is merchant M1's available balance right now?", LIVE_STATE),
        ("Why was my payout refused with LIMIT_EXCEEDED?", DOCS),
        ("What does knowledge UNKNOWN mean on my payout?", DOCS),
        ("Does a 202 mean the merchant was paid?", DOCS),
        ("What is the PLN to EUR exchange rate today?", DOCS),  # left to the scope gate
    ],
)
def test_routes(question, route):
    assert classify(question) == route
