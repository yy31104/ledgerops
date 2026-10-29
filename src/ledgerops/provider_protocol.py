"""Ledger-side verification of the synthetic provider's signed v1 observations.

This is deliberately independent of provider_sim. Verification establishes the
configured provider identity and exact request binding, not financial settlement.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

from ledgerops.domain import ProviderNamespaceId

MAX_RESPONSE_BYTES = 65536
_SIGNATURE_DOMAIN = b"ledgerops.provider-event:v1\n"
_COMMON = {"kind", "issuer", "contract_version", "namespace", "provider_key", "state"}


class InvalidObservation(ValueError):
    """An untrusted message failed authentication or the complete binding check."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidObservation("duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise InvalidObservation("nonfinite JSON number")


def _object(raw):
    if type(raw) not in (bytes, str):
        raise InvalidObservation("JSON bytes or text required")
    try:
        if len(raw if type(raw) is bytes else raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise InvalidObservation("message too large")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict:
            raise InvalidObservation("JSON object required")
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise InvalidObservation("invalid JSON object") from exc


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidObservation("canonical UUID required") from exc


@dataclass(frozen=True, slots=True)
class ProviderTrust:
    namespace: ProviderNamespaceId
    keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self):
        if type(self.namespace) is not ProviderNamespaceId:
            raise TypeError("provider trust requires typed namespace")
        if not isinstance(self.keys, Mapping) or not 1 <= len(self.keys) <= 8:
            raise ValueError("provider trust requires one to eight signing keys")
        copied = dict(self.keys)
        if any(
            type(key) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key)
            or type(secret) is not bytes
            or not 32 <= len(secret) <= 512
            for key, secret in copied.items()
        ):
            raise ValueError("invalid provider signing configuration")
        object.__setattr__(self, "keys", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    kind: str
    state: str
    event_id: str
    source_sequence: int
    key_id: str
    payload: str
    signature: str

    @property
    def signed_bytes(self):
        return json.dumps(
            {"key_id": self.key_id, "payload": self.payload, "signature": self.signature},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


def verify_observation(raw, *, trust, provider_key, request_bytes):
    """Authenticate exact signed bytes, then bind every execution request field."""
    if type(trust) is not ProviderTrust or type(request_bytes) is not bytes:
        raise TypeError("trusted configuration and retained request bytes required")
    envelope = _object(raw)
    if envelope.keys() != {"key_id", "payload", "signature"}:
        raise InvalidObservation("invalid signed envelope fields")
    key_id, payload, signature = (envelope[k] for k in ("key_id", "payload", "signature"))
    if (
        type(key_id) is not str
        or key_id not in trust.keys
        or type(payload) is not str
        or type(signature) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        raise InvalidObservation("untrusted signature identity")
    try:
        signed = payload.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidObservation("invalid signed text encoding") from exc
    if len(signed) > MAX_RESPONSE_BYTES:
        raise InvalidObservation("signed message too large")
    expected = hmac.new(
        trust.keys[key_id], _SIGNATURE_DOMAIN + key_id.encode() + b"\n" + signed, hashlib.sha256
    )
    if not hmac.compare_digest(expected.hexdigest(), signature):
        raise InvalidObservation("invalid signature")
    value = _object(payload)
    if (
        value.get("issuer") != "ledgerops-provider-sim"
        or type(value.get("contract_version")) is not int
        or value["contract_version"] != 1
        or value.get("namespace") != str(trust.namespace)
        or value.get("provider_key") != provider_key
    ):
        raise InvalidObservation("provider identity or request key mismatch")
    expected_request = _object(request_bytes)
    expected_key = (
        f"v1.{trust.namespace}.{expected_request.get('workspace_id')}."
        f"{expected_request.get('operation_id')}"
    )
    if provider_key != expected_key:
        raise InvalidObservation("retained request does not match provider key")
    if value.get("kind") == "lookup":
        if (
            value.keys() != _COMMON | {"observation_id", "visibility_watermark"}
            or value.get("state") != "NOT_FOUND"
        ):
            raise InvalidObservation("invalid absence observation")
        _uuid(value["observation_id"])
        watermark = value["visibility_watermark"]
        if type(watermark) is not int or not 0 <= watermark <= 2**63 - 1:
            raise InvalidObservation("invalid lookup watermark")
        return ProviderObservation(
            "lookup", "NOT_FOUND", value["observation_id"], watermark, key_id, payload, signature
        )
    if value.get("kind") != "execution" or value.keys() != _COMMON | {
        "event_id",
        "source_sequence",
        "request_sha256",
        "request",
    }:
        raise InvalidObservation("invalid execution fields")
    _uuid(value["event_id"])
    sequence = value["source_sequence"]
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise InvalidObservation("invalid execution sequence")
    if type(value["state"]) is not str or value["state"] not in {
        "ACCEPTED",
        "SUCCEEDED",
        "FAILED",
        "CLOSED_ABSENT",
    }:
        raise InvalidObservation("invalid execution state")
    try:
        actual_request = json.dumps(
            value["request"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidObservation("invalid execution request") from exc
    if (
        value["request_sha256"] != hashlib.sha256(request_bytes).hexdigest()
        or actual_request != request_bytes
    ):
        raise InvalidObservation("execution request binding mismatch")
    return ProviderObservation(
        "execution", value["state"], value["event_id"], sequence, key_id, payload, signature
    )
