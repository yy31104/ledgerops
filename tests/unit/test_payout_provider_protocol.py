"""Independent literal protocol signatures and bounded-client negative cases."""

import hashlib
import hmac
import json
import socket
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from ledgerops.domain import ProviderNamespaceId
from ledgerops.provider_client import ProviderHTTPClient, TransportResponse
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust, verify_observation

NAMESPACE = "10000000-0000-4000-8000-000000000001"
WORKSPACE = "10000000-0000-4000-8000-000000000002"
OPERATION = "10000000-0000-4000-8000-000000000003"
KEY = f"v1.{NAMESPACE}.{WORKSPACE}.{OPERATION}"
SECRET = b"independent-protocol-test-secret-32bytes"
TRUST = ProviderTrust(ProviderNamespaceId.parse(NAMESPACE), {"rotation1": SECRET})
REQUEST = {
    "workspace_id": WORKSPACE,
    "operation_id": OPERATION,
    "merchant_id": "10000000-0000-4000-8000-000000000004",
    "beneficiary_id": "10000000-0000-4000-8000-000000000005",
    "principal_minor": "60",
    "fee_minor": "2",
    "gross_minor": "62",
    "currency": "PLN",
    "contract_version": 1,
    "dispatch_deadline": "2026-10-29T12:01:00Z",
}
RAW_REQUEST = json.dumps(REQUEST, sort_keys=True, separators=(",", ":")).encode()


def event():
    return {
        "kind": "execution",
        "issuer": "ledgerops-provider-sim",
        "contract_version": 1,
        "namespace": NAMESPACE,
        "provider_key": KEY,
        "state": "SUCCEEDED",
        "event_id": "10000000-0000-4000-8000-000000000006",
        "source_sequence": 1,
        "request_sha256": hashlib.sha256(RAW_REQUEST).hexdigest(),
        "request": dict(REQUEST),
    }


def signed(value, *, secret=SECRET, key_id="rotation1", payload=None):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")) if payload is None else payload
    signature = hmac.new(
        secret,
        b"ledgerops.provider-event:v1\n" + key_id.encode() + b"\n" + raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(
        {"key_id": key_id, "payload": raw, "signature": signature}, separators=(",", ":")
    ).encode()


def verify(raw):
    return verify_observation(raw, trust=TRUST, provider_key=KEY, request_bytes=RAW_REQUEST)


def test_exact_literal_signed_success_and_semantic_envelope_whitespace():
    message = signed(event())
    result = verify(message)
    assert (result.kind, result.state, result.event_id, result.source_sequence) == (
        "execution",
        "SUCCEEDED",
        "10000000-0000-4000-8000-000000000006",
        1,
    )
    assert (
        verify(json.dumps(json.loads(message), indent=2).encode()).signed_bytes
        == result.signed_bytes
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "another-provider"),
        ("namespace", str(uuid4())),
        ("provider_key", KEY + "0"),
        ("contract_version", True),
        ("contract_version", 2),
        ("kind", "history_page"),
        ("state", "TIMEOUT"),
        ("source_sequence", True),
        ("source_sequence", 0),
        ("source_sequence", 2**63),
        ("event_id", "not-an-id"),
        ("request_sha256", "0" * 64),
    ],
)
def test_valid_signature_does_not_excuse_wrong_binding(field, value):
    message = event()
    message[field] = value
    with pytest.raises(InvalidObservation):
        verify(signed(message))


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_minor", "61"),
        ("fee_minor", "0"),
        ("gross_minor", "60"),
        ("merchant_id", str(uuid4())),
        ("beneficiary_id", str(uuid4())),
        ("operation_id", str(uuid4())),
        ("workspace_id", str(uuid4())),
        ("currency", "USD"),
        ("contract_version", True),
        ("dispatch_deadline", "2026-10-29T12:02:00Z"),
    ],
)
def test_every_execution_request_field_is_exactly_bound(field, value):
    message = event()
    message["request"][field] = value
    with pytest.raises(InvalidObservation):
        verify(signed(message))


@pytest.mark.parametrize(
    "malformed",
    [
        b"{}",
        b"[]",
        b'{"key_id":"a","key_id":"b"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b"\xff",
        b"x" * 65537,
    ],
)
def test_malformed_or_unbounded_envelopes_are_rejected(malformed):
    with pytest.raises(InvalidObservation):
        verify(malformed)


def test_signature_key_rotation_unknown_key_and_payload_changes():
    assert verify(signed(event())).state == "SUCCEEDED"
    with pytest.raises(InvalidObservation):
        verify(signed(event(), secret=b"other-independent-secret-32bytes!!"))
    with pytest.raises(InvalidObservation):
        verify(signed(event(), key_id="unknown"))
    envelope = json.loads(signed(event()))
    envelope["payload"] += " "
    with pytest.raises(InvalidObservation):
        verify(json.dumps(envelope).encode())
    keys = {"rotation1": SECRET}
    config = ProviderTrust(ProviderNamespaceId.parse(NAMESPACE), keys)
    keys["rotation1"] = b"changed"
    assert config.keys["rotation1"] == SECRET
    assert SECRET.decode() not in repr(config)
    with pytest.raises(TypeError):
        config.keys["rotation1"] = b"changed"


def test_signed_duplicate_keys_nonfinite_and_unknown_fields_are_rejected():
    raw = json.dumps(event(), separators=(",", ":"))
    for payload in [
        raw[:-1] + ',"state":"FAILED"}',
        raw.replace('"source_sequence":1', '"source_sequence":NaN'),
    ]:
        with pytest.raises(InvalidObservation):
            verify(signed({}, payload=payload))
    message = event()
    message["extra"] = "cannot ignore"
    with pytest.raises(InvalidObservation):
        verify(signed(message))


def test_not_found_has_only_lookup_authority_and_fixed_context():
    message = {
        "kind": "lookup",
        "issuer": "ledgerops-provider-sim",
        "contract_version": 1,
        "namespace": NAMESPACE,
        "provider_key": KEY,
        "state": "NOT_FOUND",
        "observation_id": "10000000-0000-4000-8000-000000000007",
        "visibility_watermark": 0,
    }
    result = verify(signed(message))
    assert result.kind == "lookup" and result.state == "NOT_FOUND" and result.source_sequence == 0
    for change in [
        {"visibility_watermark": True},
        {"visibility_watermark": -1},
        {"state": "FAILED"},
        {"request": REQUEST},
    ]:
        with pytest.raises(InvalidObservation):
            verify(signed({**message, **change}))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "http://example.com:80",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8080",
        "http://127.0.0.1:8080/path",
        "http://127.0.0.1:8080?token=secret",
        "http://127.0.0.1:8080#fragment",
    ],
)
def test_synthetic_client_rejects_untrusted_or_nonloopback_endpoints(url):
    with pytest.raises(ValueError):
        ProviderHTTPClient(url, "x" * 32)


@pytest.mark.parametrize("token", ["short", "x" * 513, "x" * 31 + "\n", "x" * 31 + "你"])
def test_client_credential_is_bounded_ascii(token):
    with pytest.raises(ValueError):
        ProviderHTTPClient("http://127.0.0.1:8000", token)


@pytest.mark.parametrize("timeout", [True, 0, -1, 31, float("nan"), float("inf")])
def test_client_timeout_must_be_bounded(timeout):
    with pytest.raises(ValueError):
        ProviderHTTPClient("http://127.0.0.1:8000", "x" * 32, timeout=timeout)


def test_bounded_transport_response_rejects_nonbytes_and_bad_status():
    result = TransportResponse(200, b"{}")
    for changes in [
        {"http_status": True},
        {"http_status": 99},
        {"body": "{}"},
        {"body": b"x" * 65537},
    ]:
        with pytest.raises(ValueError):
            replace(result, **changes)


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_total_deadline_interrupts_real_slow_dripping_response(stage):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    stop = threading.Event()
    accepted = threading.Event()
    errors = []

    def serve():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                conn.recv(32768)
                accepted.set()
                if stage == "body":
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"
                    )
                    stream = b"x" * 100
                else:
                    stream = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
                for byte in stream:
                    if stop.wait(0.02):
                        break
                    conn.sendall(bytes([byte]))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    client = ProviderHTTPClient(
        f"http://127.0.0.1:{listener.getsockname()[1]}", "x" * 32, timeout=0.15
    )
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client.lookup(KEY)
        assert accepted.is_set()
        assert time.monotonic() - start < 1.5
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors
