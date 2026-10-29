"""Signed fixed-watermark history rejects missing coverage before financial recovery."""

import copy
import hashlib
import hmac
import json

import pytest

from ledgerops.backup import canonical
from ledgerops.domain import ProviderNamespaceId
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust
from ledgerops.recovery import capture_history
from provider_sim.store import ProviderStore
from tests.provider.test_process import NAMESPACE, SECRET, command, key_for

TRUST = ProviderTrust(ProviderNamespaceId.parse(NAMESPACE), {"test-v1": SECRET})


def sign(payload):
    raw = canonical(payload)
    signature = hmac.new(SECRET, b"ledgerops.provider-event:v1\ntest-v1\n" + raw, hashlib.sha256)
    return {"key_id": "test-v1", "payload": raw.decode(), "signature": signature.hexdigest()}


@pytest.fixture
def store(tmp_path):
    store = ProviderStore(tmp_path / "provider.sqlite3", NAMESPACE, "test-v1", SECRET)
    for number in range(1, 4):
        body = command(number)
        store.put(key_for(body), body)
    return store


def test_fixed_watermark_excludes_later_arrival_and_next_capture_includes_it(store):
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        page = store.history(**kwargs)
        if len(requests) == 1:
            body = command(9)
            store.put(key_for(body), body)
        return canonical(page)

    snapshot = capture_history(fetch, trust=TRUST, page_limit=1)
    assert snapshot.watermark == 3 and len(snapshot.events) == 3
    assert [row["watermark"] for row in requests] == [None, 3, 3]
    later = capture_history(lambda **kw: canonical(store.history(**kw)), trust=TRUST, page_limit=2)
    assert later.watermark == 4 and len(later.events) == 4


@pytest.mark.parametrize(
    "fault",
    [
        "page_signature",
        "event_signature",
        "missing",
        "reorder",
        "watermark",
        "false_complete",
        "proof_membership",
        "proof_types",
        "missing_key",
        "duplicate",
        "empty_incomplete",
    ],
)
def test_invalid_or_incomplete_history_is_rejected(store, fault):
    def fetch(**kwargs):
        page = copy.deepcopy(store.history(**kwargs))
        if fault == "page_signature":
            page["proof"]["signature"] = "0" * 64
        elif fault == "event_signature":
            page["events"][0]["signature"] = "0" * 64
        elif fault == "missing":
            page["events"].pop(0)
        elif fault == "reorder":
            page["events"] = list(reversed(page["events"]))
        elif fault == "watermark" and kwargs["after"]:
            page["watermark"] += 1
        elif fault == "false_complete":
            page["complete"] = True
        elif fault == "proof_membership":
            proof = json.loads(page["proof"]["payload"])
            proof["event_payload_hashes"] = []
            page["proof"] = sign(proof)
        elif fault == "proof_types":
            proof = json.loads(page["proof"]["payload"])
            proof["after"] = False
            page["proof"] = sign(proof)
        elif fault == "missing_key":
            page["proof"]["key_id"] = "lost-old-key"
        elif fault == "duplicate":
            page["events"][1] = page["events"][0]
        elif fault == "empty_incomplete":
            page["events"] = []
            page["next_after"] = kwargs["after"]
        return canonical(page)

    with pytest.raises(InvalidObservation):
        capture_history(fetch, trust=TRUST, page_limit=2)


def test_authentic_proof_cannot_hide_sequence_gap(store):
    def fetch(**kwargs):
        page = store.history(**kwargs)
        page["events"] = page["events"][1:]
        page["next_after"] = kwargs["after"] + len(page["events"])
        page["complete"] = page["next_after"] == page["watermark"]
        proof = json.loads(page["proof"]["payload"])
        proof.update(
            next_after=page["next_after"],
            complete=page["complete"],
            event_payload_hashes=[
                hashlib.sha256(row["payload"].encode()).hexdigest() for row in page["events"]
            ],
        )
        page["proof"] = sign(proof)
        return canonical(page)

    with pytest.raises(InvalidObservation, match="sequence"):
        capture_history(fetch, trust=TRUST, page_limit=2)


def test_empty_namespace_requires_authenticated_complete_zero_watermark(tmp_path):
    store = ProviderStore(tmp_path / "empty.sqlite3", NAMESPACE, "test-v1", SECRET)
    snapshot = capture_history(lambda **kw: canonical(store.history(**kw)), trust=TRUST)
    assert snapshot.events == () and snapshot.watermark == 0


def test_history_limits_are_enforced(store, monkeypatch):
    import ledgerops.recovery as recovery

    monkeypatch.setattr(recovery, "MAX_HISTORY_EVENTS", 2)
    with pytest.raises(InvalidObservation, match="event limit"):
        capture_history(lambda **kw: canonical(store.history(**kw)), trust=TRUST, page_limit=2)


def test_exact_history_artifact_round_trip_and_tamper_rejection(store, tmp_path):
    from ledgerops.recovery import load_history, save_history

    snapshot = capture_history(
        lambda **kw: canonical(store.history(**kw)), trust=TRUST, page_limit=1
    )
    path = tmp_path / "history.json"
    ref = save_history(snapshot, path)
    assert ref == {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_history(path, trust=TRUST) == snapshot
    with pytest.raises(ValueError, match="NEW_HISTORY_DESTINATION_REQUIRED"):
        save_history(snapshot, path)
    artifact = json.loads(path.read_bytes())
    artifact["pages"].pop()
    path.write_bytes(canonical(artifact))
    with pytest.raises(InvalidObservation, match="incomplete retained"):
        load_history(path, trust=TRUST)
