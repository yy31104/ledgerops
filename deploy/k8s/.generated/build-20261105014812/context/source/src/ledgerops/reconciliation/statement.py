"""Bounded independent statement authentication; no provider simulator imports."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BYTES = 16 * 1024 * 1024
MAX_EFFECTS = 10_000
MAX_ROWS = 1_000
MAX_MONEY = 10**38 - 1
DOMAIN = b"ledgerops.provider-statement:v1\n"
FEE_RULE = "quoted_fee_v1"


class CorruptInput(ValueError):
    """Stable public error code; raw input and parser exceptions are not reported."""


def _fail(code):
    raise CorruptInput(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: _fail("NONFINITE_JSON"),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, CorruptInput):
            raise
        raise CorruptInput("INVALID_JSON") from exc


def canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise CorruptInput("NONCANONICAL_JSON_VALUE") from exc


def fields(value, names):
    if type(value) is not dict or value.keys() != set(names.split()):
        _fail("INVALID_FIELDS")


def text(value, maximum=128):
    if type(value) is not str or not 1 <= len(value) <= maximum or not value.isascii():
        _fail("INVALID_TEXT")
    return value


def uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            _fail("INVALID_UUID")
    except (ValueError, AttributeError) as exc:
        raise CorruptInput("INVALID_UUID") from exc
    return value


def integer(value, low=0, high=2**63 - 1):
    if type(value) is not int or not low <= value <= high:
        _fail("INVALID_INTEGER")
    return value


def money(value):
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,37}", value):
        _fail("INVALID_MONEY")
    return int(value)


def currency(value):
    if type(value) is not str or not re.fullmatch(r"[A-Z]{3}", value):
        _fail("INVALID_CURRENCY")
    return value


def instant(value):
    if type(value) is not str or len(value) > 40:
        _fail("INVALID_INSTANT")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptInput("INVALID_INSTANT") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("UTC_INSTANT_REQUIRED")
    return parsed


def utc_seconds(value):
    if type(value) is not str or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        _fail("CANONICAL_UTC_SECONDS_REQUIRED")
    return instant(value)


def day_bounds(day, timezone="Europe/Warsaw"):
    if timezone != "Europe/Warsaw":
        _fail("UNSUPPORTED_BUSINESS_TIMEZONE")
    if type(day) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        _fail("INVALID_BUSINESS_DATE")
    try:
        value = date.fromisoformat(day)
        zone = ZoneInfo(timezone)
        start = datetime.combine(value, time(), zone).astimezone(UTC)
        end = datetime.combine(value + timedelta(days=1), time(), zone).astimezone(UTC)
        return tuple(
            point.isoformat(timespec="seconds").replace("+00:00", "Z") for point in (start, end)
        )
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise CorruptInput("INVALID_BUSINESS_DATE") from exc


def provider_key(value):
    text(value, 120)
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != "v1":
        _fail("INVALID_PROVIDER_KEY")
    for part in parts[1:]:
        uuid(part)
    return tuple(parts[1:])


@dataclass(frozen=True, slots=True)
class StatementTrust:
    namespace: str
    keys: object = field(repr=False)

    def __post_init__(self):
        uuid(self.namespace)
        try:
            keys = dict(self.keys)
        except (ValueError, TypeError) as exc:
            raise CorruptInput("INVALID_STATEMENT_TRUST") from exc
        if not 1 <= len(keys) <= 8:
            _fail("INVALID_STATEMENT_TRUST")
        for name, secret in keys.items():
            if (
                type(name) is not str
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
                or type(secret) is not bytes
                or not 32 <= len(secret) <= 512
            ):
                _fail("INVALID_STATEMENT_TRUST")
        object.__setattr__(self, "keys", MappingProxyType(keys))


def _request(value):
    fields(
        value,
        "workspace_id operation_id merchant_id beneficiary_id principal_minor fee_minor "
        "gross_minor currency contract_version dispatch_deadline",
    )
    for name in ("workspace_id", "operation_id", "merchant_id", "beneficiary_id"):
        uuid(value[name])
    for name in ("principal_minor", "fee_minor", "gross_minor"):
        money(value[name])
    currency(value["currency"])
    integer(value["contract_version"], 1, 1)
    utc_seconds(value["dispatch_deadline"])


def _effect(value):
    fields(value, "provider_key execution_sequence request_sha256 request assignment")
    provider_key(value["provider_key"])
    integer(value["execution_sequence"], 1)
    if type(value["request_sha256"]) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", value["request_sha256"]
    ):
        _fail("INVALID_REQUEST_DIGEST")
    _request(value["request"])
    assignment = value["assignment"]
    if assignment is not None:
        fields(assignment, "sequence batch_id row_id effective_at recorded_at fee_rule_version")
        integer(assignment["sequence"], 1)
        for name in ("batch_id", "row_id"):
            if assignment[name] is not None:
                uuid(assignment[name])
        utc_seconds(assignment["effective_at"])
        utc_seconds(assignment["recorded_at"])
        text(assignment["fee_rule_version"])


def _row(value):
    fields(value, "row_id group provider_keys principal_minor fee_minor gross_minor")
    uuid(value["row_id"])
    group = value["group"]
    if group is not None:
        fields(group, "namespace batch_id currency merchant_id fee_rule_version")
        for name in ("namespace", "batch_id", "merchant_id"):
            if group[name] is not None:
                uuid(group[name])
        currency(group["currency"])
        text(group["fee_rule_version"])
    keys = value["provider_keys"]
    if keys is not None:
        if type(keys) is not list or len(keys) > MAX_EFFECTS:
            _fail("INVALID_MEMBER_LIST")
        for key in keys:
            provider_key(key)
    for name in ("principal_minor", "fee_minor", "gross_minor"):
        money(value[name])


def parse_statement(raw, *, trust):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        _fail("STATEMENT_BYTE_LIMIT")
    if type(trust) is not StatementTrust:
        _fail("INVALID_STATEMENT_TRUST")
    envelope = decode(raw)
    fields(envelope, "key_id payload signature")
    key_id = text(envelope["key_id"], 64)
    payload = envelope["payload"]
    signature = envelope["signature"]
    if type(payload) is not str or not payload.isascii():
        _fail("INVALID_SIGNED_PAYLOAD")
    if type(signature) is not str or not re.fullmatch(r"[0-9a-f]{64}", signature):
        _fail("INVALID_STATEMENT_SIGNATURE")
    key = trust.keys.get(key_id)
    if key is None:
        _fail("UNTRUSTED_STATEMENT_KEY")
    payload_raw = payload.encode("ascii")
    expected = hmac.new(
        key, DOMAIN + key_id.encode() + b"\n" + payload_raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        _fail("INVALID_STATEMENT_SIGNATURE")
    value = decode(payload_raw)
    fields(
        value,
        "kind issuer schema_version scope time_basis namespace workspace_id business_date "
        "timezone start_inclusive end_exclusive version generated_at effect_watermark "
        "assignment_watermark fee_rule_version rows effects",
    )
    if (
        value["kind"] != "settlement_statement"
        or value["issuer"] != "ledgerops-provider-sim"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != "COMPLETE_WORKSPACE_EFFECTS_AT_WATERMARK"
        or value["time_basis"] != "SETTLEMENT_EFFECTIVE_TIME"
        or canonical(value) != payload_raw
    ):
        _fail("UNSUPPORTED_STATEMENT_CONTRACT")
    uuid(value["namespace"])
    uuid(value["workspace_id"])
    if value["namespace"] != trust.namespace:
        _fail("UNTRUSTED_STATEMENT_NAMESPACE")
    bounds = day_bounds(value["business_date"], value["timezone"])
    if (value["start_inclusive"], value["end_exclusive"]) != bounds:
        _fail("INVALID_BUSINESS_WINDOW")
    integer(value["version"], 1, 2**31 - 1)
    integer(value["effect_watermark"])
    integer(value["assignment_watermark"])
    utc_seconds(value["generated_at"])
    text(value["fee_rule_version"])
    if type(value["effects"]) is not list or len(value["effects"]) > MAX_EFFECTS:
        _fail("STATEMENT_EFFECT_LIMIT")
    if type(value["rows"]) is not list or len(value["rows"]) > MAX_ROWS:
        _fail("STATEMENT_ROW_LIMIT")
    for effect in value["effects"]:
        _effect(effect)
    member_count = 0
    for row in value["rows"]:
        _row(row)
        member_count += len(row["provider_keys"] or ())
    if member_count > MAX_EFFECTS:
        _fail("STATEMENT_MEMBER_LIMIT")
    return value
