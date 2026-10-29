"""Read original intent and literal entries independently of posting/domain code."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from .statement import CorruptInput, currency, decode, fields, integer, money, uuid

_NAMES = {
    "PURCHASE": (
        "workspace_id operation_id customer_account_id merchant_account_id amount_minor currency"
    ),
    "REVERSAL": "workspace_id operation_id original_operation_id reason",
    "PAYOUT": (
        "workspace_id operation_id merchant_account_id beneficiary_id provider_namespace "
        "principal_minor fee_minor currency contract_version"
    ),
}
_MAX_AMOUNT = 9_000_000_000_000_000


def retained_request(row, workspace):
    raw = bytes.fromhex(row["payload_hex"])
    if hashlib.sha256(raw).hexdigest() != row["payload_sha256"]:
        raise CorruptInput("INTENT_DIGEST_MISMATCH")
    kind = row["kind"]
    prefix = f"ledgerops.{kind.lower()}:v1\n".encode()
    if not raw.startswith(prefix):
        raise CorruptInput("INVALID_INTENT_PREFIX")
    request = decode(raw[len(prefix) :])
    names = _NAMES[kind]
    fields(request, names)
    for name in names.split():
        if name.endswith("_id") or name == "provider_namespace":
            uuid(request[name])
    if request["workspace_id"] != workspace or request["operation_id"] != row["operation_id"]:
        raise CorruptInput("INTENT_IDENTITY_MISMATCH")
    if kind == "REVERSAL":
        reason = request["reason"]
        if (
            type(reason) is not str
            or "\0" in reason
            or not 1 <= len(reason.encode()) <= 256
            or request["operation_id"] == request["original_operation_id"]
        ):
            raise CorruptInput("INVALID_REVERSAL_INTENT")
    else:
        currency(request["currency"])
        amount_name = "amount_minor" if kind == "PURCHASE" else "principal_minor"
        if not 1 <= money(request[amount_name]) <= _MAX_AMOUNT:
            raise CorruptInput("INVALID_INTENT_AMOUNT")
        if kind == "PAYOUT":
            integer(request["contract_version"], 1, 1)
            if money(request["principal_minor"]) + money(request["fee_minor"]) > _MAX_AMOUNT:
                raise CorruptInput("INVALID_INTENT_AMOUNT")
    expected = (
        prefix
        + json.dumps(
            {name: request[name] for name in names.split()}, separators=(",", ":")
        ).encode()
    )
    if expected != raw:
        raise CorruptInput("NONCANONICAL_INTENT")
    return request


@dataclass
class LedgerFacts:
    workspace: str
    accounts: dict
    clearing: tuple
    operations: dict
    intents: dict
    journals: dict
    entries: dict
    payouts: dict
    resolutions: dict


def collect_ledger(snapshot, issues):
    if snapshot["format_version"] not in (6, 7, 8):
        raise CorruptInput("RECONCILIATION_REQUIRES_PAYOUT_EXPORT")
    workspace = snapshot["workspace"]["id"]
    accounts = {row["id"]: row for row in snapshot["accounts"]}
    operations = {row["operation_id"]: row for row in snapshot["operations"]}
    intents = {}
    for identifier, operation in operations.items():
        if operation["kind"] == "OPENING":
            continue  # Fixture opening has no original business-request contract.
        try:
            intents[identifier] = retained_request(operation, workspace)
        except (CorruptInput, ValueError, TypeError, KeyError) as exc:
            code = str(exc) if isinstance(exc, CorruptInput) else "INVALID_RETAINED_INTENT"
            issues.add("INPUT_CONTRADICTION", identifier, "retained_intent", "valid", code)
    journals, entries = defaultdict(list), defaultdict(list)
    for row in snapshot["journals"]:
        journals[row["operation_id"]].append(row)
    for row in snapshot["entries"]:
        entries[row["journal_id"]].append(row)
    return LedgerFacts(
        workspace,
        accounts,
        tuple(key for key, account in accounts.items() if account["class"] == "SYNTHETIC_CLEARING"),
        operations,
        intents,
        dict(journals),
        dict(entries),
        {row["operation_id"]: row for row in snapshot["payouts"]},
        {row["payout_id"]: row for row in snapshot["payout_resolutions"]},
    )


def payout_key(facts, operation_id):
    intent = facts.intents[operation_id]
    return f"v1.{intent['provider_namespace']}.{facts.workspace}.{operation_id}"


def expected_entries(facts, identifier):
    operation = facts.operations[identifier]
    intent = facts.intents[identifier]
    kind = operation["kind"]
    if kind == "REVERSAL":
        original = facts.operations.get(intent["original_operation_id"])
        intent = facts.intents.get(intent["original_operation_id"])
        if original is None or original["kind"] != "PURCHASE" or intent is None:
            raise CorruptInput("INVALID_REVERSAL_ORIGINAL")
        if original["admission"] != "ACCEPTED":
            raise CorruptInput("INVALID_REVERSAL_ORIGINAL")
        return [
            (intent["merchant_account_id"], "D", intent["amount_minor"]),
            (intent["customer_account_id"], "C", intent["amount_minor"]),
        ]
    if kind == "PURCHASE":
        return [
            (intent["customer_account_id"], "D", intent["amount_minor"]),
            (intent["merchant_account_id"], "C", intent["amount_minor"]),
        ]
    clearing = facts.clearing
    if len(clearing) != 1:
        raise CorruptInput("INVALID_CLEARING_ACCOUNT")
    gross = str(int(intent["principal_minor"]) + int(intent["fee_minor"]))
    return [(intent["merchant_account_id"], "D", gross), (clearing[0], "C", gross)]


def inspect_journal(facts, identifier, expected, issues):
    headers = facts.journals.get(identifier, [])
    if len(headers) != 1:
        return False
    header = headers[0]
    actual = sorted(
        (row["account_id"], row["side"], row["amount_minor"])
        for row in facts.entries.get(header["journal_id"], [])
    )
    expected = sorted(expected)
    if [row[:2] for row in actual] != [row[:2] for row in expected]:
        issues.add("WRONG_OWNER", identifier, "journal_entries", expected, actual)
        return False
    if actual != expected:
        issues.add("WRONG_AMOUNT", identifier, "journal_entries", expected, actual)
        return False
    request = facts.intents[identifier]
    if facts.operations[identifier]["kind"] == "REVERSAL":
        request = facts.intents[request["original_operation_id"]]
    currencies = sorted(
        {facts.accounts[row[0]]["currency"] for row in expected if row[0] in facts.accounts}
    )
    if currencies != [request["currency"]]:
        issues.add(
            "CURRENCY_MISMATCH", identifier, "account_currencies", [request["currency"]], currencies
        )
        return False
    return True


def inspect_internal(facts, provider_keys, issues, *, identifiers=None):
    """An intent is not itself proof of a payout effect: external success is separate."""
    correct = set()
    selected = set(facts.operations) | set(facts.journals) if identifiers is None else identifiers
    for identifier in sorted(selected):
        operation = facts.operations.get(identifier)
        headers = facts.journals.get(identifier, [])
        if operation is not None and operation["kind"] == "OPENING":
            continue
        if operation is None or operation["admission"] != "ACCEPTED":
            if headers:
                issues.add("UNEXPECTED_INTERNAL", identifier, "journal_count", 0, len(headers))
            continue
        if identifier not in facts.intents:
            continue
        kind = operation["kind"]
        resolution = facts.resolutions.get(identifier)
        successful = resolution is not None and resolution["state"] == "SUCCEEDED"
        provider_paid = kind == "PAYOUT" and payout_key(facts, identifier) in provider_keys
        needs_journal = kind != "PAYOUT" or successful or provider_paid
        if kind == "PAYOUT" and headers and not successful:
            issues.add(
                "UNEXPECTED_INTERNAL",
                identifier,
                "resolution",
                "SUCCEEDED",
                None if resolution is None else resolution["state"],
            )
        if len(headers) > 1:
            issues.add("DUPLICATE_EFFECT", identifier, "journal_count", 1, len(headers))
            continue
        if needs_journal and not headers:
            issues.add("MISSING_INTERNAL", identifier, "journal_count", 1, 0)
            continue
        if not headers:
            continue
        try:
            expected = expected_entries(facts, identifier)
        except CorruptInput as exc:
            issues.add(
                "INPUT_CONTRADICTION", identifier, "expected_entries", "valid intent", str(exc)
            )
            continue
        if inspect_journal(facts, identifier, expected, issues):
            correct.add(identifier)
    return correct
