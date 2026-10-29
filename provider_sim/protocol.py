"""Versioned provider protocol using only the Python standard library.

This module deliberately does not import LedgerOps. Signature verification proves
message origin under the configured simulator key, not monetary correctness.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime
from uuid import UUID

MAX_AMOUNT = 9_000_000_000_000_000
MAX_MESSAGE_BYTES = 64 * 1024
DOMAIN = b"ledgerops.provider-event:v1\n"
REQUEST_FIELDS = frozenset(
    {
        "workspace_id",
        "operation_id",
        "merchant_id",
        "beneficiary_id",
        "principal_minor",
        "fee_minor",
        "gross_minor",
        "currency",
        "contract_version",
        "dispatch_deadline",
    }
)


class ProviderError(ValueError):
    def __init__(self, code, http_status, message):
        self.code, self.http_status = code, http_status
        super().__init__(message)


class ValidationError(ProviderError):
    def __init__(self, message="Invalid provider request"):
        super().__init__("INVALID_REQUEST", 400, message)


class Conflict(ProviderError):
    def __init__(self, message="Provider identity belongs to a different request"):
        super().__init__("IDENTITY_CONFLICT", 409, message)


class NotFound(ProviderError):
    def __init__(self, message="Provider execution not found"):
        super().__init__("NOT_FOUND", 404, message)


class StoreBusy(ProviderError):
    def __init__(self):
        super().__init__("STORE_BUSY", 503, "Provider storage is busy; retry the same identity")


def canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("Invalid JSON value") from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("Duplicate JSON key")
        result[key] = value
    return result


def decode_json(data):
    if not isinstance(data, (str, bytes)):
        raise ValidationError("JSON bytes or text required")
    try:
        size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
        if size > MAX_MESSAGE_BYTES:
            raise ValidationError("Message too large")

        def reject_constant(_):
            raise ValidationError("Nonstandard JSON number")

        value = json.loads(data, object_pairs_hook=_object, parse_constant=reject_constant)
        if type(value) is not dict:
            raise ValidationError("JSON object required")
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("Invalid JSON object") from exc


def canonical_uuid(value):
    if type(value) is not str:
        raise ValidationError("Canonical UUID string required")
    try:
        if str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise ValidationError("Canonical UUID string required") from exc
    return value


def make_key(namespace, workspace_id, operation_id):
    return ".".join(("v1", *(canonical_uuid(v) for v in (namespace, workspace_id, operation_id))))


def validate_key(key, namespace):
    if type(key) is not str or len(key) > 128:
        raise ValidationError("Invalid provider key")
    parts = key.split(".")
    if len(parts) != 4 or parts[0] != "v1" or parts[1] != namespace:
        raise ValidationError("Provider key namespace/version mismatch")
    for value in parts[1:]:
        canonical_uuid(value)
    return parts[2], parts[3]


def _amount(value, minimum):
    if type(value) is not str or len(value) > 16 or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValidationError("Canonical integer amount string required")
    amount = int(value)
    if not minimum <= amount <= MAX_AMOUNT:
        raise ValidationError("Amount out of range")
    return amount


def validate_request(key, request, namespace):
    workspace, operation = validate_key(key, namespace)
    if type(request) is not dict or request.keys() != REQUEST_FIELDS:
        raise ValidationError("Request has missing or unknown fields")
    for field in ("workspace_id", "operation_id", "merchant_id", "beneficiary_id"):
        canonical_uuid(request[field])
    if (request["workspace_id"], request["operation_id"]) != (workspace, operation):
        raise ValidationError("Request does not match provider key")
    principal = _amount(request["principal_minor"], 1)
    fee = _amount(request["fee_minor"], 0)
    gross = _amount(request["gross_minor"], 1)
    if principal + fee != gross:
        raise ValidationError("Gross does not equal principal plus fee")
    if request["currency"] != "PLN":
        raise ValidationError("Only synthetic PLN is supported")
    if type(request["contract_version"]) is not int or request["contract_version"] != 1:
        raise ValidationError("Unsupported contract version")
    deadline = request["dispatch_deadline"]
    if type(deadline) is not str or not re.fullmatch(
        r"[0-9]{4}(-[0-9]{2}){2}T[0-9]{2}(:[0-9]{2}){2}Z", deadline
    ):
        raise ValidationError("Deadline must be a canonical UTC instant")
    try:
        datetime.strptime(deadline, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValidationError("Invalid deadline") from exc
    # Deadline schedules the ledger's close request; it is not proof of closure.
    return canonical_json(request).encode("utf-8")


def validate_signer(key_id, secret):
    if type(key_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
        raise ValidationError("Invalid key ID")
    if type(secret) is not bytes or len(secret) < 32:
        raise ValidationError("Signing secret must contain at least 32 bytes")


def sign_event(payload, key_id, secret):
    validate_signer(key_id, secret)
    raw = canonical_json(payload)
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValidationError("Event too large")
    signature = hmac.new(secret, DOMAIN + key_id.encode() + b"\n" + encoded, hashlib.sha256)
    return {"key_id": key_id, "payload": raw, "signature": signature.hexdigest()}


def verify_event(envelope, key_id, secret):
    """Verify origin and exact bytes only; the consumer must check business bindings."""
    validate_signer(key_id, secret)
    if type(envelope) is not dict or envelope.keys() != {"key_id", "payload", "signature"}:
        raise ValidationError("Invalid signed envelope")
    raw, signature = envelope["payload"], envelope["signature"]
    if (
        envelope["key_id"] != key_id
        or type(raw) is not str
        or type(signature) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        raise ValidationError("Invalid signed envelope")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Invalid event encoding") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValidationError("Event too large")
    expected = hmac.new(secret, DOMAIN + key_id.encode() + b"\n" + encoded, hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), signature):
        raise ProviderError("INVALID_SIGNATURE", 401, "Invalid provider signature")
    return decode_json(raw)
