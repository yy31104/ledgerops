from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from ledgerops.domain import AccountId, ActorId, ClientId, OperationId, RequestKey, WorkspaceId

ID_TEXT = "a0000000-0000-4000-8000-000000000001"
OTHER_TEXT = "a0000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize("kind", [WorkspaceId, ActorId, ClientId, AccountId, OperationId])
def test_id_round_trip_preserves_exact_uuid(kind):
    identity = kind.parse(ID_TEXT)
    assert str(identity) == ID_TEXT
    assert identity.value == UUID(ID_TEXT)
    with pytest.raises(FrozenInstanceError):
        identity.value = UUID(OTHER_TEXT)


@pytest.mark.parametrize(
    "value",
    [
        ID_TEXT.upper(),
        ID_TEXT.replace("-", ""),
        "{" + ID_TEXT + "}",
        "urn:uuid:" + ID_TEXT,
        " " + ID_TEXT,
        ID_TEXT + "\n",
        "not-a-uuid",
    ],
)
def test_noncanonical_uuid_spellings_are_rejected(value):
    with pytest.raises(ValueError):
        WorkspaceId.parse(value)


@pytest.mark.parametrize("value", [UUID(ID_TEXT), 1, True, None, ID_TEXT.encode()])
def test_wire_ids_require_text(value):
    with pytest.raises(TypeError):
        WorkspaceId.parse(value)


def test_ids_are_runtime_distinct_and_constructor_requires_uuid():
    assert WorkspaceId.parse(ID_TEXT) != AccountId.parse(ID_TEXT)
    assert ActorId.parse(ID_TEXT) != ClientId.parse(ID_TEXT)
    with pytest.raises(TypeError):
        WorkspaceId(ID_TEXT)


def test_request_key_scope_and_exact_spelling():
    workspace = WorkspaceId.parse(ID_TEXT)
    client = ClientId.parse(ID_TEXT)
    first = RequestKey(workspace, client, "key")
    assert first == RequestKey(workspace, client, "key")
    assert first != RequestKey(WorkspaceId.parse(OTHER_TEXT), client, "key")
    assert first != RequestKey(workspace, ClientId.parse(OTHER_TEXT), "key")
    assert first != RequestKey(workspace, client, "KEY")
    assert first != RequestKey(workspace, client, " key ")
    assert RequestKey(workspace, client, "é") != RequestKey(workspace, client, "e\u0301")
    assert first.route_family == "purchase:v1"
    assert RequestKey(workspace, client, "key", route_family="payout:v1") != first
    with pytest.raises(ValueError):
        RequestKey(workspace, client, "key", route_family="payout:v2")
    with pytest.raises(TypeError):
        RequestKey(workspace, ActorId.parse(ID_TEXT), "key")


@pytest.mark.parametrize("key", ["a" * 128, "é" * 64, "\u4e2d" * 42 + "aa"])
def test_request_key_length_counts_utf8_bytes(key):
    assert RequestKey(WorkspaceId.parse(ID_TEXT), ClientId.parse(ID_TEXT), key).key == key


@pytest.mark.parametrize("key", ["", "a" * 129, "é" * 65, "\u4e2d" * 43, "\x00", "x\ud800"])
def test_request_key_rejects_empty_oversize_or_unstorable_utf8(key):
    with pytest.raises(ValueError):
        RequestKey(WorkspaceId.parse(ID_TEXT), ClientId.parse(ID_TEXT), key)
