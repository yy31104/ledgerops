"""Independent checks over exported raw rows, using only the standard library.

Purchase intent is the immutable stored canonical request, not reconstructed journal
output. OPENING has no canonical request contract in R1: it receives structural
checks only. A test-only external manifest can additionally check request capture.
"""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from ledgerops.verification_external import verify_external_records

_MAX_AMOUNT = 9_000_000_000_000_000
_MAX_TOTAL = 10**38 - 1
_MAX_ROWS = 10_000
_PREFIX = b"ledgerops.purchase:v1\n"
_REVERSAL_PREFIX = b"ledgerops.reversal:v1\n"
_PAYOUT_PREFIX = b"ledgerops.payout:v1\n"
_TABLES = ("accounts", "balances", "operations", "requests", "journals", "entries")


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    object_id: str
    field: str
    expected: object
    actual: object


@dataclass(frozen=True)
class VerificationReport:
    structure_ok: bool
    projection_ok: bool
    request_intent_ok: bool
    reservation_supported: bool
    issues: tuple[VerificationIssue, ...]
    intent_scope: tuple[str, ...] = ("PURCHASE",)
    unverified_intent_kinds: tuple[str, ...] = ()
    external_evidence: dict | None = None

    @property
    def healthy(self) -> bool:
        """Posted structure, projection and supported stored-request intent agree."""
        return (
            self.structure_ok
            and self.projection_ok
            and self.request_intent_ok
            and (
                self.external_evidence is None or self.external_evidence["external_consistency_ok"]
            )
        )

    @property
    def rebuild_eligible(self) -> bool:
        """Projection drift may be repaired; invalid source/intent or reserves may not."""
        return self.structure_ok and self.request_intent_ok and self.reservation_supported

    def as_dict(self) -> dict:
        result = asdict(self)
        result["issues"] = [asdict(issue) for issue in self.issues]
        result["intent_scope"] = list(self.intent_scope)
        result["unverified_intent_kinds"] = list(self.unverified_intent_kinds)
        if self.external_evidence is None:
            del result["external_evidence"]
        else:
            result["report_version"] = 2
        result.update(healthy=self.healthy, rebuild_eligible=self.rebuild_eligible)
        return result


class _Invalid(ValueError):
    pass


def _uuid(value):
    if type(value) is not str:
        raise _Invalid("canonical UUID string required")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise _Invalid("canonical UUID string required") from exc
    if str(parsed) != value:
        raise _Invalid("canonical UUID string required")
    return value


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _Invalid("bounded integer required")
    return value


def _money(value, minimum=0, maximum=_MAX_TOTAL):
    if (
        type(value) is not str
        or len(value) > 38
        or re.fullmatch(r"0|[1-9][0-9]*", value, flags=re.ASCII) is None
    ):
        raise _Invalid("canonical integer monetary string required")
    number = int(value)
    if not minimum <= number <= maximum:
        raise _Invalid("monetary value out of bounds")
    return number


def _time(value):
    if type(value) is not str:
        raise _Invalid("UTC timestamp required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except ValueError as exc:
        raise _Invalid("UTC timestamp required") from exc
    if offset is None or offset.total_seconds() != 0:
        raise _Invalid("UTC timestamp required")
    return parsed


def _same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid("duplicate JSON key")
        result[key] = value
    return result


def _payload(row):
    encoded, digest = row["payload_hex"], row["payload_sha256"]
    if (
        type(encoded) is not str
        or not encoded
        or len(encoded) > 32 * 1024 * 1024
        or re.fullmatch(r"(?:[0-9a-f]{2})+", encoded) is None
        or type(digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise _Invalid("hex payload and SHA-256 required")
    raw = bytes.fromhex(encoded)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise _Invalid("payload digest mismatch")
    return raw


def _purchase(raw, workspace, operation_id):
    if not raw.startswith(_PREFIX):
        raise _Invalid("unsupported purchase canonical prefix")
    try:
        fields = json.loads(raw[len(_PREFIX) :], object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise _Invalid("invalid purchase JSON") from exc
    names = (
        "workspace_id",
        "operation_id",
        "customer_account_id",
        "merchant_account_id",
        "amount_minor",
        "currency",
    )
    if type(fields) is not dict or set(fields) != set(names):
        raise _Invalid("purchase fields differ from v1 contract")
    for name in names[:4]:
        _uuid(fields[name])
    if (
        fields["workspace_id"] != workspace
        or fields["operation_id"] != operation_id
        or fields["currency"] != "PLN"
        or fields["customer_account_id"] == fields["merchant_account_id"]
    ):
        raise _Invalid("purchase identity/currency mismatch")
    _money(fields["amount_minor"], 1, _MAX_AMOUNT)
    canonical = _PREFIX + json.dumps(
        {name: fields[name] for name in names}, separators=(",", ":")
    ).encode("utf-8")
    if canonical != raw:
        raise _Invalid("noncanonical purchase bytes")
    return fields


def _reversal(raw, workspace, operation_id):
    if not raw.startswith(_REVERSAL_PREFIX):
        raise _Invalid("unsupported reversal canonical prefix")
    try:
        fields = json.loads(raw[len(_REVERSAL_PREFIX) :], object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise _Invalid("invalid reversal JSON") from exc
    names = ("workspace_id", "operation_id", "original_operation_id", "reason")
    if type(fields) is not dict or set(fields) != set(names):
        raise _Invalid("reversal fields differ from v1 contract")
    for name in names[:3]:
        _uuid(fields[name])
    _reason(fields["reason"])
    if (
        fields["workspace_id"] != workspace
        or fields["operation_id"] != operation_id
        or fields["operation_id"] == fields["original_operation_id"]
    ):
        raise _Invalid("reversal identity mismatch")
    canonical = _REVERSAL_PREFIX + json.dumps(
        {name: fields[name] for name in names}, separators=(",", ":")
    ).encode("utf-8")
    if canonical != raw:
        raise _Invalid("noncanonical reversal bytes")
    return fields


class _Check:
    def __init__(self):
        self.flags = dict(structure=True, projection=True, intent=True, reservation=True)
        self.issues = []
        self.unverified = set()
        self.intent_scope = ("PURCHASE",)
        self.external_evidence = None

    def issue(self, section, code, object_id, field, expected, actual):
        self.flags[section] = False
        self.issues.append(VerificationIssue(code, str(object_id), field, expected, actual))

    def report(self):
        return VerificationReport(
            self.flags["structure"],
            self.flags["projection"],
            self.flags["intent"],
            self.flags["reservation"],
            tuple(self.issues),
            intent_scope=self.intent_scope,
            unverified_intent_kinds=tuple(sorted(self.unverified)),
            external_evidence=self.external_evidence,
        )

    def rows(self, snapshot, table, workspace):
        rows = snapshot.get(table)
        if type(rows) is not list or len(rows) > _MAX_ROWS:
            self.issue("structure", "INVALID_TABLE", table, table, "bounded row list", None)
            return []
        valid = []
        for row in rows:
            if type(row) is not dict or row.get("workspace_id") != workspace:
                self.issue(
                    "structure",
                    "CROSS_SCOPE",
                    table,
                    "workspace_id",
                    workspace,
                    row.get("workspace_id") if type(row) is dict else None,
                )
                continue
            valid.append(row)
        return valid

    def index(self, rows, table, key):
        result = {}
        for row in rows:
            try:
                value = key(row)
                hash(value)
            except (KeyError, TypeError, ValueError) as exc:
                self.issue("structure", "INVALID_ROW", table, "identity", str(exc), None)
                continue
            if value in result:
                self.issue("structure", "DUPLICATE_ROW", value, table, "unique identity", value)
            else:
                result[value] = row
        return result


def _check_accounts(check, rows):
    accounts = check.index(rows, "accounts", lambda r: _uuid(r["id"]))
    for identifier, row in list(accounts.items()):
        try:
            _uuid(row["owner_id"])
            if (
                row["currency"] != "PLN"
                or type(row["enabled"]) is not bool
                or (row["class"], row["normal_side"])
                not in {
                    ("SYNTHETIC_CLEARING", "D"),
                    ("CUSTOMER_LIABILITY", "C"),
                    ("MERCHANT_LIABILITY", "C"),
                }
            ):
                raise _Invalid("invalid account contract")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_ACCOUNT", identifier, "account", str(exc), None)
            del accounts[identifier]
    owner_classes = [(r["class"], r["owner_id"]) for r in accounts.values()]
    if len(set(owner_classes)) != len(owner_classes):
        check.issue("structure", "DUPLICATE_OWNER_CLASS", "accounts", "owner/class", "unique", None)
    return accounts


def _check_journals(check, journal_rows, entry_rows, accounts, operations):
    journals = check.index(journal_rows, "journals", lambda r: _uuid(r["journal_id"]))
    lines = defaultdict(list)
    line_keys, journal_accounts = set(), set()
    totals = {identifier: {"D": 0, "C": 0} for identifier in accounts}
    by_operation = defaultdict(list)
    for identifier, row in list(journals.items()):
        try:
            op = _uuid(row["operation_id"])
            _integer(row["line_count"], 2, 16)
            _money(row["total_minor"], 1)
            _time(row["recorded_at"])
            if op not in operations or row["kind"] != operations[op]["kind"]:
                raise _Invalid("journal operation/kind missing or different")
            by_operation[op].append(row)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_JOURNAL", identifier, "journal", str(exc), None)
            del journals[identifier]
    for row in entry_rows:
        try:
            journal_id, account = _uuid(row["journal_id"]), _uuid(row["account_id"])
            number = _integer(row["line_no"], 1, 16)
            amount = _money(row["amount_minor"], 1, _MAX_AMOUNT)
            side = row["side"]
            if journal_id not in journals or account not in accounts or side not in ("D", "C"):
                raise _Invalid("entry journal/account/side invalid")
            if (journal_id, number) in line_keys or (journal_id, account) in journal_accounts:
                raise _Invalid("duplicate entry line or account in journal")
            line_keys.add((journal_id, number))
            journal_accounts.add((journal_id, account))
            lines[journal_id].append(row)
            totals[account][side] += amount
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_ENTRY", row.get("journal_id", "?"), "entry", str(exc), None
            )
    for identifier, header in journals.items():
        actual = lines[identifier]
        debit = sum(int(r["amount_minor"]) for r in actual if r["side"] == "D")
        credit = sum(int(r["amount_minor"]) for r in actual if r["side"] == "C")
        expected = [header["line_count"], header["total_minor"], header["total_minor"]]
        observed = [len(actual), str(debit), str(credit)]
        if expected != observed:
            check.issue(
                "structure",
                "JOURNAL_DECLARATION_MISMATCH",
                identifier,
                "lines/debits/credits",
                expected,
                observed,
            )
    for operation_id, values in by_operation.items():
        if len(values) != 1:
            check.issue("structure", "DUPLICATE_EFFECT", operation_id, "journals", 1, len(values))
    return journals, lines, totals, by_operation


def _check_projections(check, rows, accounts, totals, held=None):
    balances = check.index(rows, "balances", lambda r: _uuid(r["account_id"]))
    for identifier in sorted(accounts.keys() - balances.keys()):
        check.issue("projection", "MISSING_PROJECTION", identifier, "account", "present", None)
    for identifier in sorted(balances.keys() - accounts.keys()):
        check.issue("projection", "EXTRA_PROJECTION", identifier, "account", None, "present")
        check.flags["structure"] = False
    for identifier, account in accounts.items():
        debit, credit = totals[identifier]["D"], totals[identifier]["C"]
        booked = debit - credit if account["normal_side"] == "D" else credit - debit
        if min(debit, credit, booked) < 0 or max(debit, credit, booked) > _MAX_TOTAL:
            check.issue(
                "structure",
                "INVALID_FOLDED_BALANCE",
                identifier,
                "debits/credits/booked",
                "bounded nonnegative totals",
                [debit, credit, booked],
            )
        if held is not None and booked < held.get(identifier, 0):
            check.issue(
                "structure",
                "INVALID_HELD_SOURCE",
                identifier,
                "folded_booked/reserved",
                "booked covers active holds",
                [str(booked), str(held.get(identifier, 0))],
            )
        row = balances.get(identifier)
        if row is None:
            continue
        try:
            _integer(row["version"])
            reserved = _money(row["reserved"])
            for field, expected in (("debits", debit), ("credits", credit), ("booked", booked)):
                actual = _money(row[field])
                if actual != expected:
                    check.issue(
                        "projection",
                        "PROJECTION_DRIFT",
                        identifier,
                        field,
                        str(expected),
                        str(actual),
                    )
            if row["normal_side"] != account["normal_side"]:
                raise _Invalid("projection normal side differs from account")
            if int(row["booked"]) < reserved:
                raise _Invalid("projection booked below reserved")
            if held is not None and reserved != held.get(identifier, 0):
                check.issue(
                    "projection",
                    "PROJECTION_DRIFT",
                    identifier,
                    "reserved",
                    str(held.get(identifier, 0)),
                    str(reserved),
                )
            elif held is None and reserved:
                check.issue(
                    "reservation",
                    "UNSUPPORTED_RESERVATION",
                    identifier,
                    "reserved",
                    "0 for rebuild",
                    str(reserved),
                )
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_PROJECTION", identifier, "projection", str(exc), None)
            check.flags["projection"] = False


def _check_operations(check, rows, version):
    operations = check.index(rows, "operations", lambda r: _uuid(r["operation_id"]))
    for identifier, row in list(operations.items()):
        try:
            _payload(row)
            _time(row["created_at"])
            kinds = ("OPENING", "PURCHASE", "REVERSAL") if version >= 4 else ("OPENING", "PURCHASE")
            if version >= 5:
                kinds += ("PAYOUT",)
            if row["kind"] not in kinds:
                raise _Invalid("unsupported operation kind for snapshot format")
            if (row["admission"], type(row["http_status"]), row["http_status"]) not in {
                ("ACCEPTED", int, 202 if row["kind"] == "PAYOUT" else 201),
                ("REJECTED", int, 422),
            } or type(row["response_body"]) is not dict:
                raise _Invalid("invalid committed operation outcome")
            json.dumps(row["response_body"], allow_nan=False)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_OPERATION", identifier, "operation", str(exc), None)
            check.flags["intent"] = False
            del operations[identifier]
    return operations


def _check_intent(check, workspace, accounts, operations, by_operation, lines):
    for identifier, operation in operations.items():
        headers = by_operation.get(identifier, [])
        if operation["kind"] == "OPENING":
            check.unverified.add("OPENING")
            if operation["admission"] != "ACCEPTED" or len(headers) != 1:
                check.issue(
                    "structure", "INVALID_OPENING", identifier, "journal", "one accepted", None
                )
                continue
            opening = lines[headers[0]["journal_id"]]
            sides = [(accounts[r["account_id"]]["class"], r["side"]) for r in opening]
            if len(sides) != 2 or sorted(sides) not in (
                [("CUSTOMER_LIABILITY", "C"), ("SYNTHETIC_CLEARING", "D")],
                [("MERCHANT_LIABILITY", "C"), ("SYNTHETIC_CLEARING", "D")],
            ):
                check.issue(
                    "structure",
                    "INVALID_OPENING",
                    identifier,
                    "directions",
                    "clearing debit and liability credit",
                    sides,
                )
            continue
        if operation["kind"] in {"REVERSAL", "PAYOUT"}:
            continue  # The original purchase and success-only relation are checked together below.
        try:
            request = _purchase(_payload(operation), workspace, identifier)
            customer = accounts.get(request["customer_account_id"])
            merchant = accounts.get(request["merchant_account_id"])
            if (
                customer is None
                or merchant is None
                or customer["class"] != "CUSTOMER_LIABILITY"
                or merchant["class"] != "MERCHANT_LIABILITY"
            ):
                raise _Invalid("purchase customer/merchant account classes invalid")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("intent", "INVALID_STORED_REQUEST", identifier, "payload", str(exc), None)
            continue
        if operation["admission"] == "REJECTED":
            if headers:
                check.issue(
                    "intent", "UNEXPECTED_EFFECT", identifier, "journal", None, len(headers)
                )
            error = operation["response_body"].get("error")
            if (
                type(error) is not dict
                or set(error) != {"code", "message", "retryable", "operation_id", "request_id"}
                or error.get("code") not in ("INSUFFICIENT_AVAILABLE", "LIMIT_EXCEEDED")
                or error.get("retryable") is not False
                or error.get("operation_id") != identifier
                or type(error.get("message")) is not str
                or not error["message"]
            ):
                check.issue(
                    "intent",
                    "INVALID_REJECTION",
                    identifier,
                    "response_body",
                    "stable rejection envelope",
                    operation["response_body"],
                )
            else:
                expected_message = {
                    "INSUFFICIENT_AVAILABLE": "Insufficient available balance",
                    "LIMIT_EXCEEDED": "Purchase would exceed ledger limits",
                }[error["code"]]
                if error["message"] != expected_message:
                    check.issue(
                        "intent",
                        "INVALID_REJECTION",
                        identifier,
                        "message",
                        expected_message,
                        error["message"],
                    )
                try:
                    _uuid(error["request_id"])
                except (ValueError, TypeError):
                    check.issue(
                        "intent", "INVALID_REJECTION", identifier, "request_id", "UUID", None
                    )
            continue
        if len(headers) != 1:
            check.issue("intent", "MISSING_EFFECT", identifier, "journal_count", 1, len(headers))
            continue
        header = headers[0]
        observed = lines[header["journal_id"]]
        expected_lines = [
            (request["customer_account_id"], "D", request["amount_minor"]),
            (request["merchant_account_id"], "C", request["amount_minor"]),
        ]
        actual_lines = [(r["account_id"], r["side"], r["amount_minor"]) for r in observed]
        if sorted(actual_lines) != sorted(expected_lines):
            code = (
                "WRONG_OWNER"
                if sorted((r[0], r[1]) for r in actual_lines)
                != sorted((r[0], r[1]) for r in expected_lines)
                else "WRONG_AMOUNT"
            )
            check.issue("intent", code, identifier, "journal_entries", expected_lines, actual_lines)
        expected_body = {
            "operation_id": identifier,
            "admission": "accepted",
            "journal_id": header["journal_id"],
            "amount_minor": request["amount_minor"],
            "currency": "PLN",
            "effect": "purchase_posted",
            "contract_version": 1,
        }
        if not _same_json(operation["response_body"], expected_body):
            check.issue(
                "intent",
                "INVALID_ACCEPTANCE",
                identifier,
                "response_body",
                expected_body,
                operation["response_body"],
            )


def _check_requests(check, rows, operations):
    requests = check.index(
        rows, "requests", lambda r: (_uuid(r["client_id"]), r["route_family"], r["key"])
    )
    seen_operations = set()
    for identity, row in requests.items():
        try:
            operation_id = _uuid(row["operation_id"])
            operation = operations.get(operation_id)
            _payload(row)
            key = row["key"]
            if (
                row["route_family"] not in {"purchase:v1", "reversal:v1", "payout:v1"}
                or type(key) is not str
                or "\x00" in key
                or not 1 <= len(key.encode("utf-8")) <= 128
                or operation is None
                or (operation["kind"], row["route_family"])
                not in {
                    ("PURCHASE", "purchase:v1"),
                    ("REVERSAL", "reversal:v1"),
                    ("PAYOUT", "payout:v1"),
                }
            ):
                raise _Invalid("invalid scoped request/operation reference")
            seen_operations.add(operation_id)
            for field in ("payload_hex", "payload_sha256", "http_status", "response_body"):
                if not _same_json(row[field], operation[field]):
                    check.issue(
                        "intent",
                        "REQUEST_OPERATION_MISMATCH",
                        operation_id,
                        field,
                        operation[field],
                        row[field],
                    )
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_REQUEST_RECORD", str(identity), "request", str(exc), None
            )
            check.flags["intent"] = False
    for identifier, operation in operations.items():
        if (
            operation["kind"] in {"PURCHASE", "REVERSAL", "PAYOUT"}
            and identifier not in seen_operations
        ):
            check.issue("intent", "MISSING_REQUEST", identifier, "request_result", "present", None)


def _reason(value):
    if type(value) is not str or "\x00" in value or not 1 <= len(value.encode("utf-8")) <= 256:
        raise _Invalid("bounded nonempty maintenance reason required")


def _original_purchase(workspace, original_id, accounts, operations, by_operation, lines):
    original = operations.get(original_id)
    if original is None or original["kind"] != "PURCHASE" or original["admission"] != "ACCEPTED":
        raise _Invalid("reversal original must be an accepted purchase")
    request = _purchase(_payload(original), workspace, original_id)
    customer = accounts.get(request["customer_account_id"])
    merchant = accounts.get(request["merchant_account_id"])
    if (
        customer is None
        or merchant is None
        or customer["class"] != "CUSTOMER_LIABILITY"
        or merchant["class"] != "MERCHANT_LIABILITY"
    ):
        raise _Invalid("original purchase account classes invalid")
    headers = by_operation.get(original_id, [])
    if len(headers) != 1:
        raise _Invalid("original purchase must have exactly one journal")
    header = headers[0]
    observed = [
        (r["account_id"], r["side"], r["amount_minor"]) for r in lines[header["journal_id"]]
    ]
    expected = [
        (request["customer_account_id"], "D", request["amount_minor"]),
        (request["merchant_account_id"], "C", request["amount_minor"]),
    ]
    body = {
        "operation_id": original_id,
        "admission": "accepted",
        "journal_id": header["journal_id"],
        "amount_minor": request["amount_minor"],
        "currency": "PLN",
        "effect": "purchase_posted",
        "contract_version": 1,
    }
    if sorted(observed) != sorted(expected) or not _same_json(original["response_body"], body):
        raise _Invalid("original purchase effect/result disagrees with stored request")
    return request


def _check_reversal_rejection(check, identifier, operation, headers):
    if headers:
        check.issue("intent", "UNEXPECTED_EFFECT", identifier, "journal", None, len(headers))
    messages = {
        "INSUFFICIENT_AVAILABLE": "Insufficient available balance",
        "LIMIT_EXCEEDED": "Reversal would exceed ledger limits",
        "ALREADY_REVERSED": "Purchase already reversed",
    }
    error = operation["response_body"].get("error")
    try:
        if (
            type(error) is not dict
            or error.keys() != {"code", "message", "retryable", "operation_id", "request_id"}
            or error["code"] not in messages
            or error["message"] != messages[error["code"]]
            or error["retryable"] is not False
            or error["operation_id"] != identifier
        ):
            raise _Invalid("invalid stable reversal rejection")
        _uuid(error["request_id"])
        return error["code"]
    except (KeyError, TypeError, ValueError) as exc:
        check.issue("intent", "INVALID_REJECTION", identifier, "response_body", str(exc), error)
        return None


def _check_reversals(check, snapshot, workspace, accounts, operations, by_operation, lines):
    attempts = check.index(
        check.rows(snapshot, "reversal_attempts", workspace),
        "reversal_attempts",
        lambda row: _uuid(row["operation_id"]),
    )
    effects = check.index(
        check.rows(snapshot, "reversal_effects", workspace),
        "reversal_effects",
        lambda row: _uuid(row["original_operation_id"]),
    )
    by_reversal = {}
    for original_id, row in effects.items():
        try:
            reversal_id = _uuid(row["reversal_operation_id"])
            reversal = operations.get(reversal_id)
            original = operations.get(original_id)
            attempt = attempts.get(reversal_id)
            if (
                reversal_id in by_reversal
                or reversal is None
                or reversal["kind"] != "REVERSAL"
                or reversal["admission"] != "ACCEPTED"
                or original is None
                or original["kind"] != "PURCHASE"
                or original["admission"] != "ACCEPTED"
                or attempt is None
                or attempt.get("original_operation_id") != original_id
            ):
                raise _Invalid("invalid or duplicate successful reversal relation")
            by_reversal[reversal_id] = original_id
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_REVERSAL_RELATION", original_id, "effect", str(exc), row
            )
            check.flags["intent"] = False
    for reversal_id, row in attempts.items():
        try:
            _uuid(row["original_operation_id"])
            operation = operations.get(reversal_id)
            if operation is None or operation["kind"] != "REVERSAL":
                raise _Invalid("attempt must belong to a reversal operation")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_REVERSAL_ATTEMPT", reversal_id, "attempt", str(exc), row
            )
            check.flags["intent"] = False
    for identifier, operation in operations.items():
        if operation["kind"] != "REVERSAL":
            continue
        try:
            request = _reversal(_payload(operation), workspace, identifier)
            original_id = request["original_operation_id"]
            attempt = attempts.get(identifier)
            if attempt is None or attempt.get("original_operation_id") != original_id:
                raise _Invalid("saved reversal attempt disagrees with canonical original")
            original = _original_purchase(
                workspace, original_id, accounts, operations, by_operation, lines
            )
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "intent",
                "INVALID_REVERSAL_ORIGINAL",
                identifier,
                "payload/original",
                str(exc),
                None,
            )
            continue
        headers = by_operation.get(identifier, [])
        if operation["admission"] == "REJECTED":
            code = _check_reversal_rejection(check, identifier, operation, headers)
            if identifier in by_reversal:
                check.issue(
                    "intent", "UNEXPECTED_EFFECT", identifier, "success_relation", None, True
                )
            if code == "ALREADY_REVERSED" and original_id not in effects:
                check.issue(
                    "intent", "INVALID_REJECTION", identifier, "success_relation", "present", None
                )
            continue
        if by_reversal.get(identifier) != original_id:
            check.issue(
                "intent",
                "MISSING_REVERSAL_EFFECT",
                identifier,
                "success_relation",
                original_id,
                None,
            )
        if len(headers) != 1:
            check.issue("intent", "MISSING_EFFECT", identifier, "journal_count", 1, len(headers))
            continue
        header = headers[0]
        expected_lines = [
            (original["merchant_account_id"], "D", original["amount_minor"]),
            (original["customer_account_id"], "C", original["amount_minor"]),
        ]
        actual_lines = [
            (row["account_id"], row["side"], row["amount_minor"])
            for row in lines[header["journal_id"]]
        ]
        if sorted(actual_lines) != sorted(expected_lines):
            code = (
                "WRONG_OWNER"
                if sorted((r[0], r[1]) for r in actual_lines)
                != sorted((r[0], r[1]) for r in expected_lines)
                else "WRONG_AMOUNT"
            )
            check.issue("intent", code, identifier, "journal_entries", expected_lines, actual_lines)
        expected_body = {
            "operation_id": identifier,
            "original_operation_id": original_id,
            "admission": "accepted",
            "journal_id": header["journal_id"],
            "amount_minor": original["amount_minor"],
            "currency": "PLN",
            "effect": "purchase_reversed",
            "contract_version": 1,
        }
        if not _same_json(operation["response_body"], expected_body):
            check.issue(
                "intent",
                "INVALID_ACCEPTANCE",
                identifier,
                "response_body",
                expected_body,
                operation["response_body"],
            )


def _payout_request(raw, workspace, operation_id):
    """Parse captured business intent independently of domain/admission helpers."""
    if not raw.startswith(_PAYOUT_PREFIX):
        raise _Invalid("unsupported payout canonical prefix")
    try:
        request = json.loads(raw[len(_PAYOUT_PREFIX) :], object_pairs_hook=_unique_json_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _Invalid("invalid payout JSON") from exc
    names = (
        "workspace_id",
        "operation_id",
        "merchant_account_id",
        "beneficiary_id",
        "provider_namespace",
        "principal_minor",
        "fee_minor",
        "currency",
        "contract_version",
    )
    if type(request) is not dict or request.keys() != set(names):
        raise _Invalid("payout fields differ from v1 contract")
    for name in names[:5]:
        _uuid(request[name])
    principal = _money(request["principal_minor"], 1, _MAX_AMOUNT)
    fee = _money(request["fee_minor"], 0, _MAX_AMOUNT)
    if (
        request["workspace_id"] != workspace
        or request["operation_id"] != operation_id
        or request["currency"] != "PLN"
        or type(request["contract_version"]) is not int
        or request["contract_version"] != 1
        or principal + fee > _MAX_AMOUNT
    ):
        raise _Invalid("invalid payout identity, amount or version")
    expected = (
        _PAYOUT_PREFIX
        + json.dumps({name: request[name] for name in names}, separators=(",", ":")).encode()
    )
    if expected != raw:
        raise _Invalid("noncanonical payout bytes")
    return request


def _provider_instruction(row, request):
    """Check stored transmission bytes against captured intent; no signature claim."""
    encoded, digest = row["provider_request_hex"], row["provider_request_sha256"]
    if (
        type(encoded) is not str
        or not re.fullmatch(r"(?:[0-9a-f]{2})+", encoded)
        or len(encoded) > 65536
        or type(digest) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise _Invalid("invalid provider instruction bytes")
    raw = bytes.fromhex(encoded)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise _Invalid("provider instruction digest mismatch")
    deadline = _time(row["dispatch_deadline"])
    if deadline.microsecond:
        raise _Invalid("provider deadline must have whole-second precision")
    deadline_text = deadline.isoformat(timespec="seconds").replace("+00:00", "Z")
    gross = str(int(request["principal_minor"]) + int(request["fee_minor"]))
    expected = {
        "workspace_id": request["workspace_id"],
        "operation_id": request["operation_id"],
        "merchant_id": request["merchant_account_id"],
        "beneficiary_id": request["beneficiary_id"],
        "principal_minor": request["principal_minor"],
        "fee_minor": request["fee_minor"],
        "gross_minor": gross,
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": deadline_text,
    }
    if raw != json.dumps(expected, sort_keys=True, separators=(",", ":")).encode():
        raise _Invalid("provider bytes differ from captured payout instruction")
    return deadline_text, gross


def _payout_rejection(operation):
    body = operation["response_body"]
    if type(body) is not dict or body.keys() != {"error"} or type(body["error"]) is not dict:
        raise _Invalid("invalid payout rejection envelope")
    error = body["error"]
    messages = {
        "INSUFFICIENT_AVAILABLE": "Insufficient available balance",
        "LIMIT_EXCEEDED": "Payout would exceed ledger limits",
    }
    if (
        error.keys() != {"code", "message", "operation_id", "request_id", "retryable"}
        or error["code"] not in messages
        or error["message"] != messages[error["code"]]
        or error["operation_id"] != operation["operation_id"]
        or error["retryable"] is not False
    ):
        raise _Invalid("invalid payout rejection fields")
    _uuid(error["request_id"])


def _payout_transport(check, snapshot, workspace, payouts, facts=None):
    intents = check.index(
        check.rows(snapshot, "dispatch_intents", workspace),
        "dispatch_intents",
        lambda row: _uuid(row["payout_id"]),
    )
    attempts = check.index(
        check.rows(snapshot, "transport_attempts", workspace),
        "transport_attempts",
        lambda row: _uuid(row["attempt_id"]),
    )
    by_payout = defaultdict(list)
    for identifier, row in attempts.items():
        try:
            payout_id = _uuid(row["payout_id"])
            if payout_id not in payouts or row["action"] not in {"PUT", "LOOKUP", "CLOSE"}:
                raise _Invalid("attempt refers to missing payout or invalid action")
            _time(row["started_at"])
            by_payout[payout_id].append(identifier)
            outcome = row["outcome"]
            if outcome not in {
                "MAY_HAVE_SENT",
                "RESPONSE",
                "TIMEOUT",
                "CONNECTION_ERROR",
                "TRANSPORT_ERROR",
                "INVALID_RESPONSE",
            }:
                raise _Invalid("invalid transport outcome")
            if outcome == "MAY_HAVE_SENT":
                if any(
                    row[name] is not None
                    for name in ("finished_at", "http_status", "response_payload_hex")
                ):
                    raise _Invalid("unfinished attempt contains a completion")
            else:
                _time(row["finished_at"])
            status = row["http_status"]
            if status is not None:
                _integer(status, 100, 599)
            payload = row["response_payload_hex"]
            if payload is not None and (
                type(payload) is not str
                or not re.fullmatch(r"(?:[0-9a-f]{2})*", payload)
                or len(payload) > 131072
            ):
                raise _Invalid("invalid bounded transport bytes")
            if outcome in {"RESPONSE", "INVALID_RESPONSE"} and (status is None or payload is None):
                raise _Invalid("response outcome lacks response evidence")
            if outcome in {"TIMEOUT", "CONNECTION_ERROR"} and (
                status is not None or payload is not None
            ):
                raise _Invalid("transport absence contains an HTTP response")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_TRANSPORT_ATTEMPT", identifier, "attempt", str(exc), None
            )
    for identifier, row in intents.items():
        try:
            if identifier not in payouts:
                raise _Invalid("intent payout is missing")
            count = _integer(row["attempts"])
            _time(row["next_due_at"])
            states = {"READY", "IN_FLIGHT", "OBSERVING"}
            if facts is not None:
                states |= {"DONE", "BLOCKED"}
            if row["state"] not in states or row["next_action"] not in {
                "PUT",
                "LOOKUP",
                "CLOSE",
            }:
                raise _Invalid("invalid dispatch state/action")
            if count != len(by_payout[identifier]):
                raise _Invalid("attempt counter differs from retained history")
            if count == 0:
                if row["last_attempt_id"] is not None:
                    raise _Invalid("no-attempt history cannot name an attempt")
                if facts is None and (row["state"] != "READY" or row["next_action"] != "PUT"):
                    raise _Invalid("initial intent state differs from no-attempt history")
                if facts is None and payouts[identifier]["knowledge"] != "NOT_SENT":
                    raise _Invalid("unsent payout has unknown knowledge")
            else:
                last = _uuid(row["last_attempt_id"])
                if last not in by_payout[identifier] or row["state"] == "READY":
                    raise _Invalid("current claim is missing or not owned by payout")
                if (facts is None and payouts[identifier]["knowledge"] != "UNKNOWN") or (
                    facts is not None and payouts[identifier]["knowledge"] == "NOT_SENT"
                ):
                    raise _Invalid("claimed payout cannot claim to be unsent")
            if facts is not None:
                if row["state"] == "READY" and (count != 0 or row["next_action"] != "PUT"):
                    raise _Invalid("ready intent must be initial unsent work")
                if row["state"] == "READY" and payouts[identifier]["knowledge"] != "NOT_SENT":
                    raise _Invalid("ready intent cannot ignore applied provider knowledge")
                if (row["state"] == "DONE") != (identifier in facts.resolutions):
                    raise _Invalid("done intent must have exactly one terminal resolution")
                if row["state"] == "BLOCKED" and identifier not in facts.incidents:
                    raise _Invalid("blocked intent lacks a retained incident")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_DISPATCH_INTENT", identifier, "intent", str(exc), None
            )
    return intents


def _payout_effect(check, identifier, request, resolution, headers, lines, accounts):
    if resolution is None or resolution["state"] != "SUCCEEDED":
        if headers:
            raise _Invalid("unresolved/negative payout must not have a financial journal")
        return
    if len(headers) != 1 or headers[0]["journal_id"] != resolution["journal_id"]:
        raise _Invalid("successful payout must bind exactly one terminal journal")
    clearing = [key for key, row in accounts.items() if row["class"] == "SYNTHETIC_CLEARING"]
    if len(clearing) != 1:
        raise _Invalid("one clearing account is required by payout contract")
    gross = str(int(request["principal_minor"]) + int(request["fee_minor"]))
    expected = [(request["merchant_account_id"], "D", gross), (clearing[0], "C", gross)]
    actual = [
        (row["account_id"], row["side"], row["amount_minor"])
        for row in lines[headers[0]["journal_id"]]
    ]
    if sorted(actual) != sorted(expected):
        code = (
            "WRONG_OWNER"
            if sorted(row[:2] for row in actual) != sorted(row[:2] for row in expected)
            else "WRONG_AMOUNT"
        )
        check.issue("intent", code, identifier, "payout_journal", expected, actual)


def _check_payouts(check, snapshot, workspace, accounts, operations, by_operation, lines):
    """Fold immutable hold history and bind local effects to captured intent."""
    payouts = check.index(
        check.rows(snapshot, "payouts", workspace),
        "payouts",
        lambda row: _uuid(row["operation_id"]),
    )
    reservations = check.index(
        check.rows(snapshot, "reservations", workspace),
        "reservations",
        lambda row: _uuid(row["payout_id"]),
    )
    events = check.index(
        check.rows(snapshot, "reservation_events", workspace),
        "reservation_events",
        lambda row: (_uuid(row["payout_id"]), _integer(row["event_no"], 1)),
    )
    history_by_payout = defaultdict(list)
    for identity in events:
        history_by_payout[identity[0]].append(identity)
    facts = (
        verify_external_records(check, snapshot, workspace, payouts)
        if snapshot["format_version"] >= 6
        else None
    )
    applied_sources = (
        set()
        if facts is None
        else {
            (row["payout_id"], row["state"], row["source_sequence"])
            for row in facts.observations.values()
            if row["kind"] == "execution"
        }
    )
    accepted_sources = {identity for identity, state, _ in applied_sources if state == "ACCEPTED"}
    intents = _payout_transport(check, snapshot, workspace, payouts, facts)
    held = defaultdict(int)
    for identifier, row in payouts.items():
        try:
            op = operations.get(identifier)
            if op is None or op["kind"] != "PAYOUT" or op["admission"] != "ACCEPTED":
                raise _Invalid("payout must bind an accepted payout operation")
            knowledge = {"NOT_SENT", "UNKNOWN"}
            if facts is not None:
                knowledge |= {"ACCEPTED", "SUCCEEDED", "FAILED", "CLOSED_ABSENT"}
            if row["knowledge"] not in knowledge:
                raise _Invalid("external finality is unsupported in v5")
            if facts is not None:
                sequence = _integer(row["source_sequence"])
                matching = (identifier, row["knowledge"], sequence) in applied_sources
                if (sequence > 0 and not matching) or (
                    row["knowledge"] not in {"NOT_SENT", "UNKNOWN"} and sequence == 0
                ):
                    raise _Invalid("current knowledge lacks a compatible source observation")
                resolution = facts.resolutions.get(identifier)
                if resolution is not None and resolution["state"] != row["knowledge"]:
                    raise _Invalid("terminal knowledge differs from committed resolution")
                if resolution is None and row["knowledge"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "CLOSED_ABSENT",
                }:
                    raise _Invalid("terminal knowledge lacks financial resolution")
                if (
                    resolution is None
                    and identifier not in facts.incidents
                    and identifier in accepted_sources
                    and row["knowledge"] != "ACCEPTED"
                ):
                    raise _Invalid("accepted observation has not advanced local knowledge")
            _integer(row["version"], 1)
            for name in ("merchant_account_id", "beneficiary_id", "provider_namespace"):
                _uuid(row[name])
            principal = _money(row["principal_minor"], 1, _MAX_AMOUNT)
            fee = _money(row["fee_minor"], 0, _MAX_AMOUNT)
            gross = _money(row["gross_minor"], 1, _MAX_AMOUNT)
            if (
                principal + fee != gross
                or row["currency"] != "PLN"
                or type(row["contract_version"]) is not int
                or row["contract_version"] != 1
            ):
                raise _Invalid("invalid payout amounts or contract")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_PAYOUT", identifier, "payout", str(exc), None)
    for identifier, reservation in reservations.items():
        try:
            payout = payouts.get(identifier)
            account = _uuid(reservation["account_id"])
            gross = _money(reservation["gross_minor"], 1, _MAX_AMOUNT)
            if (
                payout is None
                or account not in accounts
                or accounts[account]["class"] != "MERCHANT_LIABILITY"
                or type(reservation["version"]) is not int
                or account != payout["merchant_account_id"]
                or reservation["gross_minor"] != payout["gross_minor"]
            ):
                raise _Invalid("hold identity, state or amount differs from payout")
            history = sorted(history_by_payout[identifier])
            resolution = None if facts is None else facts.resolutions.get(identifier)
            expected_history = [(identifier, 1)] + (
                [(identifier, 2)] if resolution is not None else []
            )
            if history != expected_history:
                raise _Invalid("hold requires one initial and at most one terminal event")
            event = events[(identifier, 1)]
            if event["state"] != "HELD" or event["observation_id"] is not None:
                raise _Invalid("invalid initial hold event")
            _time(event["recorded_at"])
            if resolution is None:
                if reservation["state"] != "HELD" or reservation["version"] != 1:
                    raise _Invalid("unresolved hold must remain HELD at version1")
                held[account] += gross
            else:
                event = events[(identifier, 2)]
                state = "CONSUMED" if resolution["state"] == "SUCCEEDED" else "RELEASED"
                if (
                    reservation["state"] != state
                    or reservation["version"] != 2
                    or event["state"] != state
                    or event["observation_id"] != resolution["observation_id"]
                ):
                    raise _Invalid("terminal hold differs from its resolution/observation")
                _time(event["recorded_at"])
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_HOLD_HISTORY", identifier, "hold", str(exc), None)
            check.flags["reservation"] = False
    for identifier, _ in events:
        if identifier not in reservations:
            check.issue("structure", "ORPHAN_HOLD_EVENT", identifier, "hold", "present", None)
            check.flags["reservation"] = False
    for identifier, operation in operations.items():
        if operation["kind"] != "PAYOUT":
            continue
        try:
            request = _payout_request(_payload(operation), workspace, identifier)
            merchant = accounts.get(request["merchant_account_id"])
            if merchant is None or merchant["class"] != "MERCHANT_LIABILITY":
                raise _Invalid("payout merchant account is missing or wrong class")
            if facts is None and by_operation.get(identifier):
                check.issue(
                    "structure",
                    "INVALID_PAYOUT_EFFECT",
                    identifier,
                    "journals",
                    0,
                    len(by_operation[identifier]),
                )
                raise _Invalid("v5 payout must not have a financial journal")
            if operation["admission"] == "REJECTED":
                if any(identifier in table for table in (payouts, reservations, intents)):
                    raise _Invalid("rejected payout has a hold, instruction or intent")
                if by_operation.get(identifier):
                    raise _Invalid("rejected payout has a financial journal")
                _payout_rejection(operation)
                continue
            if any(identifier not in table for table in (payouts, reservations, intents)):
                check.issue(
                    "structure",
                    "INCOMPLETE_PAYOUT",
                    identifier,
                    "admission",
                    "instruction, hold and intent",
                    None,
                )
                raise _Invalid("accepted payout lacks complete instruction, hold or intent")
            row = payouts[identifier]
            for name in (
                "merchant_account_id",
                "beneficiary_id",
                "provider_namespace",
                "principal_minor",
                "fee_minor",
                "currency",
                "contract_version",
            ):
                if not _same_json(row[name], request[name]):
                    raise _Invalid(f"payout {name} differs from captured request")
            key = ".".join(("v1", request["provider_namespace"], workspace, identifier))
            if row["provider_key"] != key:
                raise _Invalid("provider key differs from stable business identity")
            deadline, gross = _provider_instruction(row, request)
            if row["gross_minor"] != gross:
                raise _Invalid("payout gross differs from captured amount and fee")
            expected_body = {
                "operation_id": identifier,
                "payout_id": identifier,
                "admission": "accepted_for_processing",
                "principal_minor": request["principal_minor"],
                "fee_minor": request["fee_minor"],
                "gross_minor": gross,
                "currency": "PLN",
                "dispatch_deadline": deadline,
                "contract_version": 1,
            }
            if not _same_json(operation["response_body"], expected_body):
                raise _Invalid("payout acceptance differs from fixed instruction")
            if facts is not None:
                _payout_effect(
                    check,
                    identifier,
                    request,
                    facts.resolutions.get(identifier),
                    by_operation.get(identifier, []),
                    lines,
                    accounts,
                )
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("intent", "INVALID_PAYOUT_INTENT", identifier, "payout", str(exc), None)
    return held


def _external_report(value):
    """Validate a saved v2 coverage report without reinterpreting old evidence."""
    if type(value) is not dict or value.keys() != {
        "signature_authenticity",
        "independent_provider_history",
        "external_consistency_ok",
        "accepted_payout_count",
        "resolved_payout_count",
        "payouts_resolved",
        "unresolved_payout_ids",
        "incident_payout_ids",
        "local_blocker_payout_ids",
        "quarantine_count",
        "retained_observation_count",
    }:
        raise _Invalid("invalid external evidence report fields")
    if (
        value["signature_authenticity"] != "NOT_VERIFIED_NO_TRUST_KEYS"
        or value["independent_provider_history"] != "NOT_PROVIDED"
        or type(value["external_consistency_ok"]) is not bool
        or type(value["payouts_resolved"]) is not bool
    ):
        raise _Invalid("unsupported external evidence verification claim")
    counts = {
        name: _integer(value[name], 0, _MAX_ROWS)
        for name in (
            "accepted_payout_count",
            "resolved_payout_count",
            "quarantine_count",
            "retained_observation_count",
        )
    }
    for name in ("unresolved_payout_ids", "incident_payout_ids", "local_blocker_payout_ids"):
        ids = value[name]
        if type(ids) is not list or len(ids) > _MAX_ROWS:
            raise _Invalid("bounded payout identity list required")
        if [_uuid(item) for item in ids] != sorted(set(ids)):
            raise _Invalid("external payout identities must be unique and sorted")
    admitted, resolved = counts["accepted_payout_count"], counts["resolved_payout_count"]
    if (
        resolved > admitted
        or len(value["unresolved_payout_ids"]) != admitted - resolved
        or value["payouts_resolved"] != (admitted == resolved)
        or len(value["incident_payout_ids"]) > admitted
        or len(value["local_blocker_payout_ids"]) > admitted
        or (not value["external_consistency_ok"] and not value["incident_payout_ids"])
    ):
        raise _Invalid("external evidence report counts/coverage disagree")


def _resume_report(report):
    """A saved review records warnings without imposing rebuild admission on resume."""
    flags = (
        "structure_ok",
        "projection_ok",
        "request_intent_ok",
        "reservation_supported",
        "healthy",
        "rebuild_eligible",
    )
    if type(report) is not dict:
        raise _Invalid("invalid saved resume report fields")
    expected_keys = {*flags, "issues", "intent_scope", "unverified_intent_kinds"}
    consistent = True
    if "report_version" in report:
        if type(report["report_version"]) is not int or report["report_version"] != 2:
            raise _Invalid("unsupported saved report version")
        expected_keys |= {"report_version", "external_evidence"}
        _external_report(report.get("external_evidence"))
        consistent = report["external_evidence"]["external_consistency_ok"]
    if report.keys() != expected_keys:
        raise _Invalid("invalid saved resume report fields")
    if any(type(report[name]) is not bool for name in flags):
        raise _Invalid("saved report flags must be booleans")
    if not report["structure_ok"] or not report["projection_ok"]:
        raise _Invalid("saved resume report must pass structure and projection checks")
    if report["healthy"] != (report["request_intent_ok"] and consistent) or report[
        "rebuild_eligible"
    ] != (report["request_intent_ok"] and report["reservation_supported"]):
        raise _Invalid("saved resume report derived flags disagree")
    for name in ("intent_scope", "unverified_intent_kinds"):
        if type(report[name]) is not list or any(type(kind) is not str for kind in report[name]):
            raise _Invalid("saved resume report coverage must be string lists")
    issues = report["issues"]
    if type(issues) is not list or len(issues) > 100_000:
        raise _Invalid("invalid saved resume report issues")
    for issue in issues:
        if (
            type(issue) is not dict
            or issue.keys() != {"code", "object_id", "field", "expected", "actual"}
            or any(type(issue[name]) is not str for name in ("code", "object_id", "field"))
        ):
            raise _Invalid("invalid saved resume issue")
    try:
        json.dumps(report, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _Invalid("saved resume report is not finite JSON") from exc


def _check_maintenance(check, snapshot, workspace, accounts):
    """Validate audit relations, not reinterpret historical financial intent."""
    workspace_id = workspace["id"]
    freezes = check.index(
        check.rows(snapshot, "maintenance_freezes", workspace_id),
        "maintenance_freezes",
        lambda row: _uuid(row["freeze_id"]),
    )
    open_freezes = set()
    for identifier, row in freezes.items():
        try:
            _uuid(row["actor_id"])
            _reason(row["reason"])
            _time(row["frozen_at"])
            resumed = (row["resumed_at"], row["resumed_by"], row["resume_reason"])
            if resumed == (None, None, None):
                open_freezes.add(identifier)
                if snapshot["format_version"] >= 3 and row["resume_report"] is not None:
                    raise _Invalid("open freeze cannot contain a saved resume report")
            elif any(value is None for value in resumed):
                raise _Invalid("partial freeze resume record")
            else:
                _time(row["resumed_at"])
                _uuid(row["resumed_by"])
                _reason(row["resume_reason"])
                # Legacy closed rows predate schema 004 and legitimately have no
                # report. New transitions require a report through their DB guard.
                if snapshot["format_version"] >= 3 and row["resume_report"] is not None:
                    _resume_report(row["resume_report"])
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_FREEZE", identifier, "freeze", str(exc), None)
    current = workspace["current_freeze_id"]
    expected_open = {current} if workspace["mode"] == "FROZEN" and current else set()
    if (
        (workspace["mode"] == "FROZEN" and current is None)
        or (workspace["mode"] != "FROZEN" and current is not None)
        or open_freezes != expected_open
    ):
        check.issue(
            "structure",
            "FREEZE_STATE_MISMATCH",
            workspace_id,
            "current_freeze_id",
            sorted(expected_open),
            sorted(open_freezes),
        )
    repairs = check.index(
        check.rows(snapshot, "repairs", workspace_id),
        "repairs",
        lambda row: _uuid(row["repair_id"]),
    )
    epochs = []
    for identifier, row in repairs.items():
        try:
            freeze = _uuid(row["freeze_id"])
            if freeze not in freezes:
                raise _Invalid("repair freeze is missing")
            _uuid(row["actor_id"])
            _reason(row["reason"])
            _uuid(row["preview_export_id"])
            _time(row["applied_at"])
            if type(row["source_witness"]) is not str or not re.fullmatch(
                r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", row["source_witness"]
            ):
                raise _Invalid("invalid repair source witness")
            old = _integer(row["old_epoch"], 1, 2**63 - 2)
            new = _integer(row["new_epoch"], 2)
            _integer(row["account_count"], 0, _MAX_ROWS)
            if snapshot["format_version"] >= 5:
                _integer(row["fold_version"], 1, 2)
            if new != old + 1:
                raise _Invalid("repair must advance epoch exactly once")
            epochs.append(old)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_REPAIR", identifier, "repair", str(exc), None)
    if len(epochs) != workspace["projection_epoch"] - 1 or any(
        old != index for index, old in enumerate(sorted(epochs), 1)
    ):
        check.issue(
            "structure",
            "REPAIR_EPOCH_GAP",
            workspace_id,
            "projection_epoch",
            "complete unique repair epoch history",
            workspace["projection_epoch"],
        )
    rows = check.rows(snapshot, "repair_rows", workspace_id)
    indexed = check.index(
        rows, "repair_rows", lambda row: (_uuid(row["repair_id"]), _uuid(row["account_id"]))
    )
    counts = defaultdict(int)
    for identity, row in indexed.items():
        try:
            repair, account_id = identity
            if repair not in repairs or account_id not in accounts:
                raise _Invalid("repair row header/account is missing")
            counts[repair] += 1
            side = row["normal_side"]
            if side != accounts[account_id]["normal_side"]:
                raise _Invalid("repair row normal side differs from account")
            after = {
                name: _money(row["after_" + name])
                for name in ("debits", "credits", "reserved", "booked")
            }
            version = _integer(row["after_version"], 1)
            booked = (
                after["debits"] - after["credits"]
                if side == "D"
                else after["credits"] - after["debits"]
            )
            fold_version = repairs[repair].get("fold_version", 1)
            if (
                (fold_version == 1 and after["reserved"] != 0)
                or after["booked"] != booked
                or after["booked"] < after["reserved"]
            ):
                raise _Invalid("invalid repaired totals/reservation")
            before_names = ("debits", "credits", "reserved", "booked", "version")
            before_values = [row["before_" + name] for name in before_names]
            if all(value is None for value in before_values):
                if version != 1:
                    raise _Invalid("new projection must start at version 1")
            else:
                before = {
                    name: _money(row["before_" + name])
                    for name in ("debits", "credits", "reserved", "booked")
                }
                old_version = _integer(row["before_version"], 0, 2**63 - 2)
                old_booked = (
                    before["debits"] - before["credits"]
                    if side == "D"
                    else before["credits"] - before["debits"]
                )
                if (
                    (fold_version == 1 and before["reserved"] != 0)
                    or before["booked"] != old_booked
                    or before["booked"] < before["reserved"]
                    or version != old_version + 1
                ):
                    raise _Invalid("invalid original totals or projection version transition")
        except (KeyError, TypeError, ValueError) as exc:
            check.issue("structure", "INVALID_REPAIR_ROW", str(identity), "audit", str(exc), None)
    for identifier, row in repairs.items():
        if counts[identifier] != row.get("account_count"):
            check.issue(
                "structure",
                "REPAIR_DECLARATION_MISMATCH",
                identifier,
                "account_count",
                row.get("account_count"),
                counts[identifier],
            )


def verify_snapshot(snapshot: dict) -> VerificationReport:
    """Check posted structure/projection and stored purchase intent without SUT helpers.

    Nonzero reservations are reported separately. R1 OPENING records have opaque
    fixture payloads, so their funding intent is explicitly unverified. This function
    neither mutates inputs nor verifies provenance beyond the supplied stored rows.
    """
    check = _Check()
    try:
        if (
            type(snapshot) is not dict
            or type(snapshot.get("format_version")) is not int
            or snapshot["format_version"] not in (1, 2, 3, 4, 5, 6)
        ):
            raise _Invalid("snapshot version 1 through 6 required")
        workspace = snapshot["workspace"]
        if type(workspace) is not dict:
            raise _Invalid("workspace object required")
        workspace_id = _uuid(workspace["id"])
        if workspace["currency"] != "PLN" or workspace["mode"] not in {
            "NORMAL",
            "FROZEN",
            "RESTORE",
        }:
            raise _Invalid("unsupported workspace contract")
        _integer(workspace["schema_epoch"], 1)
        version = snapshot["format_version"]
        schemas = ["001_foundation.sql", "002_export_reader.sql"]
        if version >= 2:
            schemas.append("003_projection_maintenance.sql")
            _integer(workspace["projection_epoch"], 1)
            if workspace["current_freeze_id"] is not None:
                _uuid(workspace["current_freeze_id"])
        if version >= 3:
            schemas.append("004_resume_review.sql")
        if version >= 4:
            schemas.append("005_full_reversal.sql")
        if version >= 5:
            schemas.append("006_payout_admission.sql")
        if version >= 6:
            schemas.append("007_payout_resolution.sql")
        if (
            snapshot["schema_versions"] != schemas
            or type(snapshot["snapshot_witness"]) is not str
            or not snapshot["snapshot_witness"]
            or type(snapshot["server_version"]) is not str
        ):
            raise _Invalid("unsupported schema/snapshot metadata")
    except (KeyError, TypeError, ValueError) as exc:
        check.issue("structure", "INVALID_SNAPSHOT", "snapshot", "metadata", str(exc), None)
        check.flags.update(projection=False, intent=False, reservation=False)
        return check.report()
    tables = {name: check.rows(snapshot, name, workspace_id) for name in _TABLES}
    if version >= 4:
        check.intent_scope = ("PURCHASE", "REVERSAL")
    if version >= 5:
        check.intent_scope += ("PAYOUT",)
        if version == 5:
            check.unverified.add("PAYOUT_EXTERNAL_RESULT")
        else:
            check.unverified.update({"PAYOUT_PROVIDER_AUTHENTICITY", "PAYOUT_EXTERNAL_EFFECT"})
    accounts = _check_accounts(check, tables["accounts"])
    if snapshot["format_version"] >= 2:
        _check_maintenance(check, snapshot, workspace, accounts)
    operations = _check_operations(check, tables["operations"], version)
    _, lines, totals, by_operation = _check_journals(
        check, tables["journals"], tables["entries"], accounts, operations
    )
    held = (
        _check_payouts(check, snapshot, workspace_id, accounts, operations, by_operation, lines)
        if version >= 5
        else None
    )
    _check_projections(check, tables["balances"], accounts, totals, held)
    _check_intent(check, workspace_id, accounts, operations, by_operation, lines)
    if version >= 4:
        _check_reversals(check, snapshot, workspace_id, accounts, operations, by_operation, lines)
    _check_requests(check, tables["requests"], operations)
    return check.report()
