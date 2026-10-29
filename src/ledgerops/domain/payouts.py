"""Fixed payout instructions and trusted admission policy, independent of transport."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ledgerops.domain.identities import (
    AccountId,
    ActorId,
    BeneficiaryId,
    OperationId,
    ProviderNamespaceId,
    WorkspaceId,
)
from ledgerops.domain.money import AmountMinor, checked_operation_total, parse_fee

PAYOUT_ROUTE = "payout:v1"
PAYOUT_PREFIX = b"ledgerops.payout:v1\n"
_FIELDS = {
    "operation_id",
    "merchant_account_id",
    "beneficiary_id",
    "provider_namespace",
    "principal_minor",
    "fee_minor",
    "currency",
    "contract_version",
}


@dataclass(frozen=True, slots=True)
class PayoutCommand:
    workspace_id: WorkspaceId
    operation_id: OperationId
    merchant_account_id: AccountId
    beneficiary_id: BeneficiaryId
    provider_namespace: ProviderNamespaceId
    principal_minor: AmountMinor
    fee_minor: int
    currency: str = "PLN"
    contract_version: int = 1

    def __post_init__(self):
        for name, cls in (
            ("workspace_id", WorkspaceId),
            ("operation_id", OperationId),
            ("merchant_account_id", AccountId),
            ("beneficiary_id", BeneficiaryId),
            ("provider_namespace", ProviderNamespaceId),
            ("principal_minor", AmountMinor),
        ):
            if type(getattr(self, name)) is not cls:
                raise TypeError(f"{name} must be {cls.__name__}")
        checked_operation_total(self.principal_minor, self.fee_minor)
        if self.currency != "PLN":
            raise ValueError("only synthetic PLN is supported")
        if type(self.contract_version) is not int or self.contract_version != 1:
            raise ValueError("unsupported payout contract version")

    @property
    def gross_minor(self):
        return self.principal_minor.value + self.fee_minor

    @classmethod
    def from_payload(cls, workspace_id, payload):
        if not isinstance(payload, dict):
            raise TypeError("payout payload must be an object")
        if payload.keys() != _FIELDS:
            raise ValueError("payout payload has missing or unknown fields")
        return cls(
            workspace_id,
            OperationId.parse(payload["operation_id"]),
            AccountId.parse(payload["merchant_account_id"]),
            BeneficiaryId.parse(payload["beneficiary_id"]),
            ProviderNamespaceId.parse(payload["provider_namespace"]),
            AmountMinor.parse(payload["principal_minor"]),
            parse_fee(payload["fee_minor"]),
            payload["currency"],
            payload["contract_version"],
        )

    @property
    def canonical_bytes(self):
        fields = {
            "workspace_id": str(self.workspace_id),
            "operation_id": str(self.operation_id),
            "merchant_account_id": str(self.merchant_account_id),
            "beneficiary_id": str(self.beneficiary_id),
            "provider_namespace": str(self.provider_namespace),
            "principal_minor": str(self.principal_minor.value),
            "fee_minor": str(self.fee_minor),
            "currency": self.currency,
            "contract_version": self.contract_version,
        }
        return PAYOUT_PREFIX + json.dumps(fields, separators=(",", ":")).encode("utf-8")

    @property
    def payload_sha256(self):
        return hashlib.sha256(self.canonical_bytes).digest()

    def matches_bytes(self, stored_payload):
        if type(stored_payload) is not bytes:
            raise TypeError("stored canonical payload must be bytes")
        return self.canonical_bytes == stored_payload


@dataclass(frozen=True, slots=True)
class TrustedPayoutPolicy:
    """Trusted configuration, never constructed from the merchant request body."""

    workspace_id: WorkspaceId
    provider_namespace: ProviderNamespaceId
    fixed_fee_minor: int
    dispatch_ttl_seconds: int
    beneficiary_owners: Mapping[BeneficiaryId, ActorId]

    def __post_init__(self):
        if type(self.workspace_id) is not WorkspaceId:
            raise TypeError("policy requires a WorkspaceId")
        if type(self.provider_namespace) is not ProviderNamespaceId:
            raise TypeError("policy requires a ProviderNamespaceId")
        checked_operation_total(AmountMinor(1), self.fixed_fee_minor)
        if (
            type(self.dispatch_ttl_seconds) is not int
            or not 1 <= self.dispatch_ttl_seconds <= 86400
        ):
            raise ValueError("dispatch window must be 1 to 86400 seconds")
        if (
            not isinstance(self.beneficiary_owners, Mapping)
            or not 1 <= len(self.beneficiary_owners) <= 10000
        ):
            raise ValueError("policy requires bounded beneficiary ownership bindings")
        copied = dict(self.beneficiary_owners)
        if any(type(k) is not BeneficiaryId or type(v) is not ActorId for k, v in copied.items()):
            raise TypeError("beneficiary bindings require typed identities")
        object.__setattr__(self, "beneficiary_owners", MappingProxyType(copied))

    def permits(self, command, actor_id):
        return (
            command.workspace_id == self.workspace_id
            and command.provider_namespace == self.provider_namespace
            and command.fee_minor == self.fixed_fee_minor
            and self.beneficiary_owners.get(command.beneficiary_id) == actor_id
        )
