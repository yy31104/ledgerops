"""Literal full-reversal wire identity, independent of posting and canonical helpers."""

from dataclasses import replace
from hashlib import sha256

import pytest

from ledgerops.domain import (
    AccountId,
    ClientId,
    OperationId,
    RequestKey,
    ReversalCommand,
    WorkspaceId,
)

WORKSPACE = "00000000-0000-4000-8000-000000000010"
PAYLOAD = {
    "operation_id": "00000000-0000-4000-8000-000000000002",
    "original_operation_id": "00000000-0000-4000-8000-000000000001",
    "reason": "merchant refund",
}
EXPECTED_BYTES = (
    b'ledgerops.reversal:v1\n{"workspace_id":"00000000-0000-4000-8000-000000000010",'
    b'"operation_id":"00000000-0000-4000-8000-000000000002",'
    b'"original_operation_id":"00000000-0000-4000-8000-000000000001",'
    b'"reason":"merchant refund"}'
)


def command(payload=None):
    return ReversalCommand.from_payload(
        WorkspaceId.parse(WORKSPACE), PAYLOAD if payload is None else payload
    )


def test_literal_reversal_bytes_digest_and_fixed_field_order():
    reversal = command()
    assert reversal.canonical_bytes == EXPECTED_BYTES
    assert reversal.payload_sha256 == sha256(EXPECTED_BYTES).digest()
    assert reversal.matches_bytes(EXPECTED_BYTES)
    assert command(dict(reversed(list(PAYLOAD.items())))).canonical_bytes == EXPECTED_BYTES


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": WorkspaceId.parse("00000000-0000-4000-8000-000000000011")},
        {"operation_id": OperationId.parse("00000000-0000-4000-8000-000000000003")},
        {"original_operation_id": OperationId.parse("00000000-0000-4000-8000-000000000004")},
        {"reason": "merchant refund "},
    ],
)
def test_every_reversal_business_field_changes_identity(changes):
    assert not replace(command(), **changes).matches_bytes(EXPECTED_BYTES)


@pytest.mark.parametrize("field", PAYLOAD)
def test_missing_fields_are_rejected(field):
    payload = dict(PAYLOAD)
    del payload[field]
    with pytest.raises(ValueError):
        command(payload)


@pytest.mark.parametrize(
    "field", ["amount_minor", "currency", "merchant_account_id", "actor_id", "request_key"]
)
def test_reversal_cannot_supply_amount_destination_or_transport_identity(field):
    with pytest.raises(ValueError):
        command({**PAYLOAD, field: "ignored?"})


@pytest.mark.parametrize("reason", ["", "\x00", "x\x00y", "x" * 257, "é" * 129, "\ud800"])
def test_reason_is_bounded_utf8_and_cannot_contain_nul(reason):
    with pytest.raises(ValueError):
        command({**PAYLOAD, "reason": reason})


@pytest.mark.parametrize("reason", [None, True, 1, [], {}])
def test_reason_requires_exact_text_type(reason):
    with pytest.raises(TypeError):
        command({**PAYLOAD, "reason": reason})


def test_reason_bytes_and_whitespace_are_preserved_without_normalization():
    assert command({**PAYLOAD, "reason": " "}).reason == " "
    assert command({**PAYLOAD, "reason": "é" * 128}).reason == "é" * 128
    composed = command({**PAYLOAD, "reason": "é"})
    decomposed = command({**PAYLOAD, "reason": "e\u0301"})
    assert composed.canonical_bytes != decomposed.canonical_bytes
    assert composed.canonical_bytes.endswith(b'"reason":"\\u00e9"}')


@pytest.mark.parametrize("payload", [[], "{}", True, 80])
def test_wire_payload_requires_an_object(payload):
    with pytest.raises(TypeError):
        command(payload)


def test_typed_ids_and_stored_bytes_remain_strict():
    with pytest.raises(TypeError):
        replace(command(), original_operation_id=AccountId.parse(PAYLOAD["original_operation_id"]))
    with pytest.raises(TypeError):
        replace(command(), workspace_id=WORKSPACE)
    with pytest.raises(TypeError):
        command().matches_bytes(bytearray(EXPECTED_BYTES))


def test_equal_operation_and_original_ids_are_left_to_authorized_business_conflict():
    same = command({**PAYLOAD, "operation_id": PAYLOAD["original_operation_id"]})
    assert same.operation_id == same.original_operation_id


def test_reversal_route_is_distinct_from_default_purchase_request_identity():
    workspace, client = WorkspaceId.parse(WORKSPACE), ClientId.parse(PAYLOAD["operation_id"])
    purchase = RequestKey(workspace, client, "same-key")
    reversal = RequestKey(workspace, client, "same-key", route_family="reversal:v1")
    assert purchase != reversal
    assert purchase.route_family == "purchase:v1"
    assert reversal.route_family == "reversal:v1"
