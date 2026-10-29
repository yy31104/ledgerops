"""Versioned business bytes and transport identity, without admission logic."""

import hashlib
import json
from dataclasses import dataclass

from ledgerops.domain.identities import AccountId, ClientId, OperationId, WorkspaceId
from ledgerops.domain.money import AmountMinor
from ledgerops.domain.payouts import PAYOUT_ROUTE

PURCHASE_ROUTE = "purchase:v1"
REVERSAL_ROUTE = "reversal:v1"
CANONICAL_PREFIX = b"ledgerops.purchase:v1\n"
REVERSAL_PREFIX = b"ledgerops.reversal:v1\n"
_PURCHASE_FIELDS = frozenset(
    {"operation_id", "customer_account_id", "merchant_account_id", "amount_minor", "currency"}
)


@dataclass(frozen=True, slots=True)
class PurchaseCommand:
    workspace_id: WorkspaceId
    operation_id: OperationId
    customer_account_id: AccountId
    merchant_account_id: AccountId
    amount_minor: AmountMinor
    currency: str = "PLN"

    def __post_init__(self) -> None:
        for name, expected_type in (
            ("workspace_id", WorkspaceId),
            ("operation_id", OperationId),
            ("customer_account_id", AccountId),
            ("merchant_account_id", AccountId),
            ("amount_minor", AmountMinor),
        ):
            if type(getattr(self, name)) is not expected_type:
                raise TypeError(f"{name} must be {expected_type.__name__}")
        if self.currency != "PLN":
            raise ValueError("only synthetic PLN is supported")
        if self.customer_account_id == self.merchant_account_id:
            raise ValueError("source and destination accounts must differ")

    @classmethod
    def from_payload(cls, workspace_id: WorkspaceId, payload: object) -> "PurchaseCommand":
        """Accept already-decoded JSON; duplicate JSON keys must be rejected upstream."""
        if not isinstance(payload, dict):
            raise TypeError("purchase payload must be an object")
        if payload.keys() != _PURCHASE_FIELDS:
            raise ValueError("purchase payload has missing or unknown fields")
        return cls(
            workspace_id=workspace_id,
            operation_id=OperationId.parse(payload["operation_id"]),
            customer_account_id=AccountId.parse(payload["customer_account_id"]),
            merchant_account_id=AccountId.parse(payload["merchant_account_id"]),
            amount_minor=AmountMinor.parse(payload["amount_minor"]),
            currency=payload["currency"],
        )

    @property
    def canonical_bytes(self) -> bytes:
        # Field order and separators are part of v1. Transport keys, client/actor
        # credentials, headers, traces and generated timestamps are excluded.
        fields = {
            "workspace_id": str(self.workspace_id),
            "operation_id": str(self.operation_id),
            "customer_account_id": str(self.customer_account_id),
            "merchant_account_id": str(self.merchant_account_id),
            "amount_minor": str(self.amount_minor.value),
            "currency": self.currency,
        }
        return CANONICAL_PREFIX + json.dumps(fields, separators=(",", ":")).encode("utf-8")

    @property
    def payload_sha256(self) -> bytes:
        """An index/integrity aid; stored bytes remain the equality authority."""
        return hashlib.sha256(self.canonical_bytes).digest()

    def matches_bytes(self, stored_payload: bytes) -> bool:
        if type(stored_payload) is not bytes:
            raise TypeError("stored canonical payload must be bytes")
        return self.canonical_bytes == stored_payload


@dataclass(frozen=True, slots=True)
class ReversalCommand:
    workspace_id: WorkspaceId
    operation_id: OperationId
    original_operation_id: OperationId
    reason: str

    def __post_init__(self) -> None:
        for name, expected_type in (
            ("workspace_id", WorkspaceId),
            ("operation_id", OperationId),
            ("original_operation_id", OperationId),
        ):
            if type(getattr(self, name)) is not expected_type:
                raise TypeError(f"{name} must be {expected_type.__name__}")
        if type(self.reason) is not str:
            raise TypeError("reversal reason must be a string")
        try:
            valid = "\x00" not in self.reason and 1 <= len(self.reason.encode("utf-8")) <= 256
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ValueError("reversal reason must contain 1 to 256 UTF-8 bytes without NUL")

    @classmethod
    def from_payload(cls, workspace_id: WorkspaceId, payload: object) -> "ReversalCommand":
        if not isinstance(payload, dict):
            raise TypeError("reversal payload must be an object")
        if payload.keys() != {"operation_id", "original_operation_id", "reason"}:
            raise ValueError("reversal payload has missing or unknown fields")
        return cls(
            workspace_id,
            OperationId.parse(payload["operation_id"]),
            OperationId.parse(payload["original_operation_id"]),
            payload["reason"],
        )

    @property
    def canonical_bytes(self) -> bytes:
        fields = {
            "workspace_id": str(self.workspace_id),
            "operation_id": str(self.operation_id),
            "original_operation_id": str(self.original_operation_id),
            "reason": self.reason,
        }
        return REVERSAL_PREFIX + json.dumps(fields, separators=(",", ":")).encode("utf-8")

    @property
    def payload_sha256(self) -> bytes:
        return hashlib.sha256(self.canonical_bytes).digest()

    def matches_bytes(self, stored_payload: bytes) -> bool:
        if type(stored_payload) is not bytes:
            raise TypeError("stored canonical payload must be bytes")
        return self.canonical_bytes == stored_payload


@dataclass(frozen=True, slots=True)
class RequestKey:
    workspace_id: WorkspaceId
    client_id: ClientId
    key: str
    route_family: str = PURCHASE_ROUTE

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not WorkspaceId or type(self.client_id) is not ClientId:
            raise TypeError("request scope requires WorkspaceId and authenticated ClientId")
        if self.route_family not in (PURCHASE_ROUTE, REVERSAL_ROUTE, PAYOUT_ROUTE):
            raise ValueError("unsupported request route family")
        if not isinstance(self.key, str):
            raise TypeError("request key must be a string")
        # PostgreSQL text cannot represent NUL. No trimming, Unicode
        # normalization, or case folding changes the submitted key.
        if "\x00" in self.key:
            raise ValueError("request key cannot contain NUL")
        try:
            length = len(self.key.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("request key must be valid UTF-8") from exc
        if not 1 <= length <= 128:
            raise ValueError("request key must contain 1 to 128 UTF-8 bytes")
