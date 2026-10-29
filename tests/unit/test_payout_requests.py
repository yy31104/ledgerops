"""Literal payout wire contract and immutable trusted-policy configuration."""

from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from ledgerops.domain import ActorId, BeneficiaryId, PayoutCommand, ProviderNamespaceId, WorkspaceId
from ledgerops.domain.payouts import TrustedPayoutPolicy

WORKSPACE = "00000000-0000-4000-8000-000000000010"
PAYLOAD = {
    "operation_id": "00000000-0000-4000-8000-000000000002",
    "merchant_account_id": "00000000-0000-4000-8000-000000000003",
    "beneficiary_id": "00000000-0000-4000-8000-000000000004",
    "provider_namespace": "00000000-0000-4000-8000-000000000005",
    "principal_minor": "60",
    "fee_minor": "2",
    "currency": "PLN",
    "contract_version": 1,
}
EXPECTED = (
    b'ledgerops.payout:v1\n{"workspace_id":"00000000-0000-4000-8000-000000000010",'
    b'"operation_id":"00000000-0000-4000-8000-000000000002",'
    b'"merchant_account_id":"00000000-0000-4000-8000-000000000003",'
    b'"beneficiary_id":"00000000-0000-4000-8000-000000000004",'
    b'"provider_namespace":"00000000-0000-4000-8000-000000000005",'
    b'"principal_minor":"60","fee_minor":"2","currency":"PLN","contract_version":1}'
)


def command(**changes):
    return PayoutCommand.from_payload(WorkspaceId.parse(WORKSPACE), {**PAYLOAD, **changes})


def test_literal_canonical_bytes_hash_and_independent_total():
    result = command()
    assert result.canonical_bytes == EXPECTED
    assert result.payload_sha256 == sha256(EXPECTED).digest()
    assert result.gross_minor == 62
    assert result.matches_bytes(EXPECTED)
    assert b"dispatch_deadline" not in result.canonical_bytes


@pytest.mark.parametrize("field", ["actor_id", "request_key", "dispatch_deadline", "gross_minor"])
def test_client_cannot_supply_transport_generated_or_derived_fields(field):
    with pytest.raises(ValueError):
        command(**{field: "ignored?"})


@pytest.mark.parametrize(
    "field", ["operation_id", "merchant_account_id", "beneficiary_id", "provider_namespace"]
)
def test_each_identity_field_changes_canonical_bytes(field):
    assert command(**{field: str(uuid4())}).canonical_bytes != EXPECTED


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_minor", 60),
        ("fee_minor", 2),
        ("fee_minor", True),
        ("fee_minor", "-1"),
        ("principal_minor", "0"),
        ("principal_minor", "60.0"),
        ("fee_minor", "02"),
        ("principal_minor", "9000000000000000"),
        ("contract_version", True),
        ("contract_version", 2),
        ("currency", "EUR"),
    ],
)
def test_invalid_or_overflowing_amounts_and_contracts_fail(field, value):
    with pytest.raises((ValueError, TypeError)):
        command(**{field: value})


def test_zero_fee_and_exact_amount_limit_are_valid():
    result = command(principal_minor="9000000000000000", fee_minor="0")
    assert result.gross_minor == 9_000_000_000_000_000


def test_policy_copies_ownership_and_refuses_mutation():
    beneficiary = BeneficiaryId.parse(PAYLOAD["beneficiary_id"])
    actor = ActorId.parse(str(uuid4()))
    owners = {beneficiary: actor}
    policy = TrustedPayoutPolicy(
        WorkspaceId.parse(WORKSPACE),
        ProviderNamespaceId.parse(PAYLOAD["provider_namespace"]),
        2,
        60,
        owners,
    )
    owners[beneficiary] = ActorId.parse(str(uuid4()))
    assert policy.permits(command(), actor)
    assert not policy.permits(command(fee_minor="3"), actor)
    with pytest.raises(TypeError):
        policy.beneficiary_owners[beneficiary] = owners[beneficiary]
    for seconds in (0, -1, 86401, True):
        with pytest.raises(ValueError):
            replace(policy, dispatch_ttl_seconds=seconds)
