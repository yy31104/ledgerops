from dataclasses import replace
from hashlib import sha256

import pytest

from ledgerops.domain import AccountId, AmountMinor, OperationId, PurchaseCommand, WorkspaceId

WORKSPACE = "00000000-0000-4000-8000-000000000010"
PAYLOAD = {
    "operation_id": "00000000-0000-4000-8000-000000000001",
    "customer_account_id": "00000000-0000-4000-8000-000000000101",
    "merchant_account_id": "00000000-0000-4000-8000-000000000201",
    "amount_minor": "80",
    "currency": "PLN",
}
# Literal oracle: neither production normalization nor a posting helper builds it.
EXPECTED_BYTES = (
    b'ledgerops.purchase:v1\n{"workspace_id":"00000000-0000-4000-8000-000000000010",'
    b'"operation_id":"00000000-0000-4000-8000-000000000001",'
    b'"customer_account_id":"00000000-0000-4000-8000-000000000101",'
    b'"merchant_account_id":"00000000-0000-4000-8000-000000000201",'
    b'"amount_minor":"80","currency":"PLN"}'
)


def command(payload=None):
    return PurchaseCommand.from_payload(
        WorkspaceId.parse(WORKSPACE), PAYLOAD if payload is None else payload
    )


def test_fixed_versioned_bytes_and_digest_match_literal_case():
    purchase = command()
    assert purchase.canonical_bytes == EXPECTED_BYTES
    assert purchase.payload_sha256 == sha256(EXPECTED_BYTES).digest()
    assert purchase.matches_bytes(EXPECTED_BYTES)
    assert command(dict(reversed(list(PAYLOAD.items())))).canonical_bytes == EXPECTED_BYTES


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": WorkspaceId.parse("00000000-0000-4000-8000-000000000011")},
        {"operation_id": OperationId.parse("00000000-0000-4000-8000-000000000002")},
        {"customer_account_id": AccountId.parse("00000000-0000-4000-8000-000000000102")},
        {"merchant_account_id": AccountId.parse("00000000-0000-4000-8000-000000000202")},
        {"amount_minor": AmountMinor(81)},
    ],
)
def test_every_business_identity_and_amount_field_changes_equality(changes):
    assert not replace(command(), **changes).matches_bytes(EXPECTED_BYTES)


def test_digest_collision_cannot_turn_changed_payload_into_a_replay(monkeypatch):
    class CollidingDigest:
        def digest(self):
            return b"x" * 32

    monkeypatch.setattr("ledgerops.domain.requests.hashlib.sha256", lambda _: CollidingDigest())
    original = command()
    changed = command({**PAYLOAD, "amount_minor": "81"})
    assert original.payload_sha256 == changed.payload_sha256
    assert not original.matches_bytes(changed.canonical_bytes)


@pytest.mark.parametrize("field", list(PAYLOAD))
def test_missing_required_fields_are_rejected(field):
    payload = dict(PAYLOAD)
    del payload[field]
    with pytest.raises(ValueError):
        command(payload)


@pytest.mark.parametrize("field", ["idempotency_key", "actor_id", "timestamp", "amount", "extra"])
def test_unknown_fields_are_rejected_instead_of_silently_excluded(field):
    with pytest.raises(ValueError):
        command({**PAYLOAD, field: "ignored?"})


@pytest.mark.parametrize("currency", ["USD", "pln", " PLN", "", None, 1])
def test_only_exact_pln_currency_is_accepted(currency):
    with pytest.raises(ValueError):
        command({**PAYLOAD, "currency": currency})


def test_same_source_destination_and_wrong_domain_types_are_rejected():
    with pytest.raises(ValueError):
        command({**PAYLOAD, "merchant_account_id": PAYLOAD["customer_account_id"]})
    with pytest.raises(TypeError):
        replace(command(), operation_id=AccountId.parse(PAYLOAD["operation_id"]))
    with pytest.raises(TypeError):
        replace(command(), amount_minor=80)


@pytest.mark.parametrize("payload", [[], "{}", True, 80])
def test_payload_must_be_a_json_object(payload):
    with pytest.raises(TypeError):
        command(payload)
