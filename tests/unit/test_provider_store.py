"""Independent SQLite/provider contract cases, not PostgreSQL correctness claims."""

import hashlib
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest

from provider_sim.protocol import (
    DOMAIN,
    Conflict,
    NotFound,
    ProviderError,
    StoreBusy,
    ValidationError,
    decode_json,
    make_key,
    sign_event,
    validate_request,
    verify_event,
)
from provider_sim.store import ProviderStore

NAMESPACE = "00000000-0000-4000-8000-000000000001"
WORKSPACE = "00000000-0000-4000-8000-000000000002"
OPERATION = "00000000-0000-4000-8000-000000000003"
KEY = f"v1.{NAMESPACE}.{WORKSPACE}.{OPERATION}"
SECRET = b"provider-unit-test-secret-32-bytes!!"
KEY_ID = "provider-test-v1"


@pytest.fixture
def request_body():
    return {
        "workspace_id": WORKSPACE,
        "operation_id": OPERATION,
        "merchant_id": "00000000-0000-4000-8000-000000000004",
        "beneficiary_id": "00000000-0000-4000-8000-000000000005",
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": "2026-10-29T12:00:00Z",
    }


@pytest.fixture
def store(tmp_path):
    return ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, KEY_ID, SECRET)


def independent_payload(envelope):
    raw = envelope["payload"].encode("utf-8")
    signed = b"ledgerops.provider-event:v1\nprovider-test-v1\n" + raw
    assert hmac.compare_digest(
        hmac.new(SECRET, signed, hashlib.sha256).hexdigest(), envelope["signature"]
    )
    assert envelope["key_id"] == "provider-test-v1"
    return json.loads(raw)


def test_durable_success_same_identity_and_exact_observation(store, request_body):
    event = store.put(KEY, request_body)
    payload = independent_payload(event)
    assert payload["state"] == "SUCCEEDED"
    assert payload["request"] == request_body
    assert payload["source_sequence"] == 1
    assert payload["namespace"] == NAMESPACE
    assert payload["provider_key"] == KEY
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT principal_minor,fee_minor,gross_minor FROM effects"
        ).fetchall() == [("60", "2", "62")]
    reopened = ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET)
    assert reopened.put(KEY, dict(reversed(list(request_body.items())))) == event
    assert reopened.lookup(KEY) == event
    assert reopened.close(KEY, request_body) == event
    assert reopened.diagnostics()["effects"] == 1
    assert reopened.diagnostics()["events"] == 1


def test_tombstone_prevents_late_put_even_after_deadline(store, request_body):
    request_body["dispatch_deadline"] = "2000-01-01T00:00:00Z"
    event = store.close(KEY, request_body)
    assert independent_payload(event)["state"] == "CLOSED_ABSENT"
    reopened = ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET)
    assert reopened.put(KEY, request_body) == event
    assert reopened.diagnostics()["effects"] == 0
    with pytest.raises(Conflict):
        reopened.resolve(KEY, "SUCCEEDED")


def test_deadline_alone_does_not_cancel_execution(store, request_body):
    request_body["dispatch_deadline"] = "2000-01-01T00:00:00Z"
    assert independent_payload(store.put(KEY, request_body))["state"] == "SUCCEEDED"
    assert store.diagnostics()["effects"] == 1


@pytest.mark.parametrize("first", ["put", "close"])
def test_key_conflict_is_preserved_for_success_and_tombstone(store, request_body, first):
    original = getattr(store, first)(KEY, request_body)
    altered = {**request_body, "principal_minor": "59", "gross_minor": "61"}
    for action in (store.put, store.close):
        with pytest.raises(Conflict):
            action(KEY, altered)
    assert store.lookup(KEY) == original
    assert store.diagnostics()["events"] == 1


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED"])
def test_accepted_cannot_be_closed_and_terminal_is_immutable(store, request_body, terminal):
    accepted = store.put(KEY, request_body, outcome="ACCEPTED")
    assert independent_payload(accepted)["state"] == "ACCEPTED"
    assert store.close(KEY, request_body) == accepted
    result = store.resolve(KEY, terminal)
    assert independent_payload(result)["source_sequence"] == 2
    assert store.lookup(KEY) == result
    assert store.put(KEY, request_body) == result
    assert store.resolve(KEY, terminal) == result
    with pytest.raises(Conflict):
        store.resolve(KEY, "FAILED" if terminal == "SUCCEEDED" else "SUCCEEDED")
    assert store.diagnostics()["effects"] == (1 if terminal == "SUCCEEDED" else 0)
    history = store.history()
    assert history["events"] == [accepted, result]


def test_absent_is_provisional_and_lag_does_not_erase_success(store, request_body):
    absent = independent_payload(store.lookup(KEY))
    assert absent["state"] == "NOT_FOUND" and absent["kind"] == "lookup"
    assert "request" not in absent and "event_id" not in absent
    assert store.diagnostics()["executions"] == 0
    event = store.put(KEY, request_body)
    lagging = independent_payload(store.lookup(KEY, force_absent=True))
    assert lagging["state"] == "NOT_FOUND" and lagging["visibility_watermark"] == 0
    assert store.lookup(KEY) == event
    assert store.diagnostics()["effects"] == 1


def test_fixed_watermark_pages_and_signed_completeness(store, request_body):
    first = store.put(KEY, request_body, outcome="ACCEPTED")
    second = store.resolve(KEY, "SUCCEEDED")
    page = store.history(limit=1)
    assert page["watermark"] == 2 and page["next_after"] == 1 and not page["complete"]
    other = {**request_body, "operation_id": str(UUID(int=77))}
    store.put(make_key(NAMESPACE, WORKSPACE, other["operation_id"]), other)
    last = store.history(after=page["next_after"], watermark=page["watermark"], limit=1)
    assert last["complete"] and last["next_after"] == 2
    assert page["events"] + last["events"] == [first, second]
    proof = independent_payload(last["proof"])
    assert proof["watermark"] == 2 and proof["after"] == 1
    assert proof["event_payload_hashes"] == [hashlib.sha256(second["payload"].encode()).hexdigest()]
    assert store.history(after=3, watermark=3)["events"] == []


def test_empty_history_has_signed_complete_zero_watermark(store):
    page = store.history()
    assert page["watermark"] == page["next_after"] == 0 and page["complete"]
    assert independent_payload(page["proof"])["event_payload_hashes"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"after": True},
        {"limit": 0},
        {"limit": 1001},
        {"watermark": 1},
        {"after": -1},
        {"watermark": False},
    ],
)
def test_bad_history_bounds(store, arguments):
    with pytest.raises(ValidationError):
        store.history(**arguments)


@pytest.mark.parametrize("point,effects", [("before_commit", 0), ("after_commit", 1)])
def test_faults_distinguish_aborted_transaction_from_lost_response(
    store, request_body, point, effects
):
    def fail(actual):
        if actual == point:
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected"):
        store.put(KEY, request_body, hook=fail)
    reopened = ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET)
    assert reopened.diagnostics()["effects"] == effects
    assert reopened.diagnostics()["events"] == effects
    assert reopened.diagnostics()["executions"] == effects
    event = reopened.put(KEY, request_body)
    assert independent_payload(event)["state"] == "SUCCEEDED"
    assert reopened.diagnostics()["effects"] == 1


def test_actual_connections_race_put_against_close(store, request_body):
    barrier = Barrier(2)

    def action(which):
        barrier.wait(timeout=3)
        return getattr(store, which)(KEY, request_body)

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(action, kind) for kind in ("put", "close")]
        events = [f.result(timeout=5) for f in futures]
    assert events[0] == events[1]
    state = independent_payload(events[0])["state"]
    assert state in {"SUCCEEDED", "CLOSED_ABSENT"}
    assert store.diagnostics()["effects"] == int(state == "SUCCEEDED")
    assert store.diagnostics()["events"] == 1


def test_busy_is_retryable_not_a_durable_business_failure(store, request_body):
    impatient = ProviderStore(store.path, NAMESPACE, KEY_ID, SECRET, busy_timeout_ms=30)
    with sqlite3.connect(store.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreBusy) as caught:
            impatient.put(KEY, request_body)
        assert caught.value.http_status == 503
        blocker.execute("ROLLBACK")
    assert impatient.diagnostics()["effects"] == 0
    assert independent_payload(impatient.put(KEY, request_body))["state"] == "SUCCEEDED"


@pytest.mark.parametrize("change", ["namespace", "key_id", "secret"])
def test_reopen_rejects_changed_namespace_or_signer(store, request_body, change):
    original = store.put(KEY, request_body)
    config = dict(namespace=NAMESPACE, key_id=KEY_ID, secret=SECRET)
    config[change] = {"namespace": str(UUID(int=999)), "key_id": "other", "secret": b"x" * 32}[
        change
    ]
    with pytest.raises(Conflict):
        ProviderStore(store.path, **config)
    assert store.lookup(KEY) == original


def test_persistence_settings_are_real(store):
    diag = store.diagnostics()
    assert diag["journal_mode"] == "delete"
    assert diag["synchronous"] == 2
    assert diag["foreign_keys"] == 1
    assert diag["busy_timeout_ms"] == 2000
    assert diag["sqlite_version"] == sqlite3.sqlite_version
    assert diag["integrity_check"] == "ok"


def test_retained_rows_reject_mutation(store, request_body):
    store.put(KEY, request_body)
    with sqlite3.connect(store.path) as conn:
        for query in (
            "DELETE FROM events",
            "DELETE FROM effects",
            "DELETE FROM executions",
            "UPDATE effects SET gross_minor='1'",
            "UPDATE events SET state='FAILED'",
            "UPDATE executions SET state='FAILED'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(query)


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_minor", True),
        ("principal_minor", "0"),
        ("principal_minor", 60),
        ("principal_minor", "060"),
        ("principal_minor", "6e1"),
        ("fee_minor", "-1"),
        ("gross_minor", "61"),
        ("gross_minor", "9000000000000001"),
        ("currency", "USD"),
        ("contract_version", True),
        ("contract_version", 2),
        ("workspace_id", str(UUID(int=20))),
        ("operation_id", str(UUID(int=21))),
        ("dispatch_deadline", "2026-02-30T12:00:00Z"),
        ("dispatch_deadline", "2026-10-29T12:00:00+00:00"),
        ("merchant_id", "00000000-0000-4000-8000-00000000000A"),
    ],
)
def test_request_validation_has_no_effect(store, request_body, field, value):
    with pytest.raises(ValidationError):
        store.put(KEY, {**request_body, field: value})
    assert store.diagnostics()["executions"] == 0


def test_unknown_fields_and_gross_sum_overflow_are_rejected(store, request_body):
    with pytest.raises(ValidationError):
        store.put(KEY, {**request_body, "ignored": "no"})
    with pytest.raises(ValidationError):
        store.put(
            KEY,
            {
                **request_body,
                "principal_minor": "9000000000000000",
                "fee_minor": "1",
                "gross_minor": "9000000000000000",
            },
        )


def test_fee_zero_and_maximum_exact_amount(store, request_body):
    request_body.update(
        principal_minor="9000000000000000", fee_minor="0", gross_minor="9000000000000000"
    )
    event = store.put(KEY, request_body)
    assert independent_payload(event)["request"]["gross_minor"] == "9000000000000000"


def test_missing_resolve_and_bad_state(store):
    with pytest.raises(NotFound):
        store.resolve(KEY, "SUCCEEDED")
    with pytest.raises(ValidationError):
        store.resolve(KEY, "CLOSED_ABSENT")


def test_canonical_wire_bytes_and_raw_signature(request_body):
    expected = json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()
    assert validate_request(KEY, request_body, NAMESPACE) == expected
    payload = {"state": "SUCCEEDED", "amount_minor": "62"}
    event = sign_event(payload, KEY_ID, SECRET)
    assert verify_event(event, KEY_ID, SECRET) == payload
    assert DOMAIN == b"ledgerops.provider-event:v1\n"
    changed = {**event, "payload": event["payload"].replace("62", "63")}
    with pytest.raises(ProviderError) as caught:
        verify_event(changed, KEY_ID, SECRET)
    assert caught.value.code == "INVALID_SIGNATURE"
    with pytest.raises(ValidationError):
        verify_event({**event, "key_id": "unknown"}, KEY_ID, SECRET)
    with pytest.raises(ValidationError):
        sign_event(payload, KEY_ID, b"short")
    with pytest.raises(ValidationError):
        decode_json('{"state":"FAILED","state":"SUCCEEDED"}')


@pytest.mark.parametrize("text", ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
def test_nonstandard_json_is_rejected(text):
    with pytest.raises(ValidationError):
        decode_json(text)
