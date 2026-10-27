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

_MAX_AMOUNT = 9_000_000_000_000_000
_MAX_TOTAL = 10**38 - 1
_MAX_ROWS = 10_000
_PREFIX = b"ledgerops.purchase:v1\n"
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

    @property
    def healthy(self) -> bool:
        """Posted structure, projection and supported stored-request intent agree."""
        return self.structure_ok and self.projection_ok and self.request_intent_ok

    @property
    def rebuild_eligible(self) -> bool:
        """Projection drift may be repaired; invalid source/intent or reserves may not."""
        return self.structure_ok and self.request_intent_ok and self.reservation_supported

    def as_dict(self) -> dict:
        result = asdict(self)
        result["issues"] = [asdict(issue) for issue in self.issues]
        result["intent_scope"] = list(self.intent_scope)
        result["unverified_intent_kinds"] = list(self.unverified_intent_kinds)
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


class _Check:
    def __init__(self):
        self.flags = dict(structure=True, projection=True, intent=True, reservation=True)
        self.issues = []
        self.unverified = set()

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
            unverified_intent_kinds=tuple(sorted(self.unverified)),
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


def _check_projections(check, rows, accounts, totals):
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
            if reserved:
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


def _check_operations(check, rows):
    operations = check.index(rows, "operations", lambda r: _uuid(r["operation_id"]))
    for identifier, row in list(operations.items()):
        try:
            _payload(row)
            _time(row["created_at"])
            if row["kind"] not in ("OPENING", "PURCHASE"):
                raise _Invalid("unsupported operation kind in snapshot v1")
            if (row["admission"], type(row["http_status"]), row["http_status"]) not in {
                ("ACCEPTED", int, 201),
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
                row["route_family"] != "purchase:v1"
                or type(key) is not str
                or "\x00" in key
                or not 1 <= len(key.encode("utf-8")) <= 128
                or operation is None
                or operation["kind"] != "PURCHASE"
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
        if operation["kind"] == "PURCHASE" and identifier not in seen_operations:
            check.issue("intent", "MISSING_REQUEST", identifier, "request_result", "present", None)


def _reason(value):
    if type(value) is not str or "\x00" in value or not 1 <= len(value.encode("utf-8")) <= 256:
        raise _Invalid("bounded nonempty maintenance reason required")


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
            elif any(value is None for value in resumed):
                raise _Invalid("partial freeze resume record")
            else:
                _time(row["resumed_at"])
                _uuid(row["resumed_by"])
                _reason(row["resume_reason"])
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
            if after["reserved"] != 0 or after["booked"] != booked:
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
                    before["reserved"] != 0
                    or before["booked"] != old_booked
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
            or snapshot["format_version"] not in (1, 2)
        ):
            raise _Invalid("snapshot version 1 or 2 required")
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
        if version == 2:
            schemas.append("003_projection_maintenance.sql")
            _integer(workspace["projection_epoch"], 1)
            if workspace["current_freeze_id"] is not None:
                _uuid(workspace["current_freeze_id"])
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
    accounts = _check_accounts(check, tables["accounts"])
    if snapshot["format_version"] == 2:
        _check_maintenance(check, snapshot, workspace, accounts)
    operations = _check_operations(check, tables["operations"])
    _, lines, totals, by_operation = _check_journals(
        check, tables["journals"], tables["entries"], accounts, operations
    )
    _check_projections(check, tables["balances"], accounts, totals)
    _check_intent(check, workspace_id, accounts, operations, by_operation, lines)
    _check_requests(check, tables["requests"], operations)
    return check.report()
