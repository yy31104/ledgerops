import json

import httpx
import pytest

from ledgerops_assistant.tools import APIUnavailable, AuthenticationRequired, ReadOnlyAPI

TOKEN = "lo1_" + "a" * 43
OBJECT = "40000000-0000-0000-0000-000000000003"
IDENTITY = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "actor_id": "00000000-0000-0000-0000-000000000002",
    "client_id": "00000000-0000-0000-0000-000000000003",
    "scopes": ["payout:read", "operations:read", "evidence:read", "accounts:read"],
}


def api(handler):
    return ReadOnlyAPI(
        "http://127.0.0.1:8080", TOKEN, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )


@pytest.mark.parametrize(
    "status,label",
    [
        (403, "forbidden"),
        (404, "not_found_or_hidden"),
        (429, "rate_limited"),
        (503, "upstream_unavailable"),
    ],
)
def test_bounded_errors_do_not_invent_absence(status, label):
    seen = []

    def handler(request):
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer " + TOKEN
        seen.append(request.url.path)
        return (
            httpx.Response(200, json=IDENTITY)
            if request.url.path == "/v1/me"
            else httpx.Response(status)
        )

    client = api(handler)
    result = client.read("payout", OBJECT, client.identity().owner_key)
    assert result["status"] == label and not result["complete"]
    assert seen.count("/v1/payouts/" + OBJECT) <= 2
    assert TOKEN not in json.dumps(result)


def test_revocation_stops_without_retry():
    client = api(lambda r: httpx.Response(401))
    with pytest.raises(AuthenticationRequired):
        client.identity()


def test_timeouts_and_rate_limit_backoff_are_bounded():
    seen = []

    def handler(request):
        if request.url.path == "/v1/me":
            return httpx.Response(200, json=IDENTITY)
        seen.append(1)
        raise httpx.ReadTimeout("synthetic", request=request)

    client = api(handler)
    assert client.read("payout", OBJECT, client.identity().owner_key)["status"] == "timeout"
    assert len(seen) == 2
    waits, calls = [], []

    def throttled(request):
        if request.url.path == "/v1/me":
            return httpx.Response(200, json=IDENTITY)
        calls.append(1)
        return (
            httpx.Response(429, headers={"Retry-After": "999"})
            if len(calls) == 1
            else httpx.Response(200, json={"knowledge": "UNKNOWN"})
        )

    client = api(throttled)
    client.sleep = waits.append
    assert client.read("payout", OBJECT, client.identity().owner_key)["status"] == "ok"
    assert waits == [2]


def test_writes_urls_redirects_and_missing_scopes_are_blocked():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={**IDENTITY, "scopes": []})

    client = api(handler)
    owner = client.identity().owner_key
    for tool, identifier in [("approve", OBJECT), ("payout", "../operator/actions")]:
        with pytest.raises((ValueError, AttributeError)):
            client.read(tool, identifier, owner)
    assert client.read("payout", OBJECT, owner)["status"] == "forbidden"
    assert set(seen) == {"/v1/me"}
    redirect = api(lambda r: httpx.Response(302, headers={"Location": "https://example.invalid"}))
    with pytest.raises(APIUnavailable):
        redirect.identity()


def test_large_payload_is_not_silently_truncated():
    client = api(
        lambda r: (
            httpx.Response(200, json=IDENTITY)
            if r.url.path == "/v1/me"
            else httpx.Response(200, content=b"x" * 70000)
        )
    )
    assert (
        client.read("payout", OBJECT, client.identity().owner_key)["status"] == "response_too_large"
    )


def test_uuid_aliases_have_canonical_routes_and_evidence_identity():
    canonical = "40000000-abcd-4000-8000-000000000003"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(
            200, json=IDENTITY if request.url.path == "/v1/me" else {"knowledge": "UNKNOWN"}
        )

    client = api(handler)
    result = client.read("payout", canonical.upper(), client.identity().owner_key)
    assert seen[-1] == "/v1/payouts/" + canonical
    assert result["object_id"] == canonical
