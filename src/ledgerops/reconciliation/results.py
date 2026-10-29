"""Validate worker summaries against pinned rows before they become a complete report."""

import hashlib
from collections import Counter

from .rules import CATEGORIES, group_key
from .statement import CorruptInput, canonical, instant, provider_key

_ISSUE_FIELDS = {"category", "object_id", "field", "expected", "actual", "scope", "refs"}
_GROUP_FIELDS = {
    "group_id",
    "group_key",
    "row_ids",
    "provider_keys",
    "principal_minor",
    "fee_minor",
    "gross_minor",
    "matched",
}


def _check_issue(issue):
    if type(issue) is not dict or issue.keys() != _ISSUE_FIELDS:
        raise CorruptInput("INVALID_DIFFERENCE_RECORD")
    if (
        issue["category"] not in CATEGORIES
        or issue["scope"] not in {"INVENTORY", "SETTLEMENT_WINDOW"}
        or any(
            type(issue[field]) is not str or not 1 <= len(issue[field]) <= 256
            for field in ("object_id", "field")
        )
        or type(issue["refs"]) is not list
        or any(type(ref) is not str or len(ref) > 256 for ref in issue["refs"])
    ):
        raise CorruptInput("INVALID_DIFFERENCE_RECORD")
    canonical(issue)


def _source_groups(prepared, units):
    expected, supported = {}, {}
    for unit in units:
        for ordinal in unit["row_ordinals"]:
            row = prepared.statement["rows"][ordinal]
            key = group_key(row["group"])
            if key is None:
                continue
            identifier = hashlib.sha256(canonical(list(key))).hexdigest()
            if identifier not in expected:
                supported[identifier] = True
                expected[identifier] = dict(
                    group_id=identifier,
                    group_key=list(key),
                    row_ids=[],
                    provider_keys=[],
                    principal_minor=0,
                    fee_minor=0,
                    gross_minor=0,
                )
            summary = expected[identifier]
            summary["row_ids"].append(row["row_id"])
            summary["provider_keys"].extend(row["provider_keys"] or ())
            for name in ("principal_minor", "fee_minor", "gross_minor"):
                summary[name] += int(row[name])
            supported[identifier] &= _matched_source_row(prepared, row, key)
    for summary in expected.values():
        summary["row_ids"].sort()
        summary["provider_keys"].sort()
        for name in ("principal_minor", "fee_minor", "gross_minor"):
            summary[name] = str(summary[name])
    return expected, supported


def _matched_source_row(prepared, row, key):
    """A signed summary is still a source claim: verify it against every member."""
    if not row["provider_keys"]:
        return False
    totals = Counter()
    start = instant(prepared.statement["start_inclusive"])
    end = instant(prepared.statement["end_exclusive"])
    for provider in row["provider_keys"]:
        effects = prepared.effects.get(provider, ())
        if len(effects) != 1:
            return False
        effect = effects[0]
        assignment, request = effect["assignment"], effect["request"]
        if assignment is None:
            return False
        expected_group = (
            prepared.statement["namespace"],
            assignment["batch_id"],
            request["currency"],
            request["merchant_id"],
            assignment["fee_rule_version"],
        )
        if (
            key != expected_group
            or assignment["row_id"] != row["row_id"]
            or not start <= instant(assignment["effective_at"]) < end
        ):
            return False
        for name in ("principal_minor", "fee_minor", "gross_minor"):
            totals[name] += int(request[name])
    return all(
        str(totals[name]) == row[name] for name in ("principal_minor", "fee_minor", "gross_minor")
    )


def _exact_key(prepared, key):
    """An advertised exact match needs literal ownership, money and one local terminal effect."""
    facts = prepared.facts
    effects = prepared.effects.get(key, ())
    if len(effects) != 1:
        return False
    identifier = provider_key(key)[2]
    operation, intent = facts.operations.get(identifier), facts.intents.get(identifier)
    headers, resolution = facts.journals.get(identifier, ()), facts.resolutions.get(identifier)
    payout = facts.payouts.get(identifier)
    if (
        operation is None
        or operation["kind"] != "PAYOUT"
        or operation["admission"] != "ACCEPTED"
        or intent is None
        or payout is None
        or len(headers) != 1
        or len(facts.clearing) != 1
        or resolution is None
        or resolution["state"] != "SUCCEEDED"
        or resolution["journal_id"] != headers[0]["journal_id"]
        or intent["provider_namespace"] != prepared.statement["namespace"]
    ):
        return False
    request = effects[0]["request"]
    expected_fields = {
        "workspace_id": facts.workspace,
        "operation_id": identifier,
        "merchant_id": intent["merchant_account_id"],
        "beneficiary_id": intent["beneficiary_id"],
        "principal_minor": intent["principal_minor"],
        "fee_minor": intent["fee_minor"],
        "gross_minor": str(int(intent["principal_minor"]) + int(intent["fee_minor"])),
        "currency": intent["currency"],
        "contract_version": intent["contract_version"],
    }
    if any(request[field] != value for field, value in expected_fields.items()):
        return False
    if instant(request["dispatch_deadline"]) != instant(payout["dispatch_deadline"]):
        return False
    actual = Counter(
        (row["account_id"], row["side"], row["amount_minor"])
        for row in facts.entries.get(headers[0]["journal_id"], ())
    )
    expected = Counter(
        (
            (intent["merchant_account_id"], "D", expected_fields["gross_minor"]),
            (facts.clearing[0], "C", expected_fields["gross_minor"]),
        )
    )
    return actual == expected


def validate_partition_result(prepared, partition, result):
    if type(result) is not dict or result.keys() != {
        "issues",
        "groups",
        "matched_provider_keys",
        "global_checks",
    }:
        raise CorruptInput("INVALID_PARTITION_RESULT")
    if any(
        type(result[name]) is not list for name in ("issues", "groups", "matched_provider_keys")
    ):
        raise CorruptInput("INVALID_PARTITION_RESULT")
    if (
        len(result["issues"]) > 100_000
        or len(result["groups"]) > 1000
        or len(result["matched_provider_keys"]) > 10_000
    ):
        raise CorruptInput("PARTITION_RESULT_LIMIT")
    metadata = prepared.input_manifest["inputs"]["ledger_data"]["sha256"] + ":ledger_metadata:0"
    expected_global = metadata in partition["coverage"]
    if type(result["global_checks"]) is not bool or result["global_checks"] != expected_global:
        raise CorruptInput("INVALID_GLOBAL_COVERAGE")
    for issue in result["issues"]:
        _check_issue(issue)
    if expected_global and not {canonical(issue) for issue in prepared.global_issues} <= {
        canonical(issue) for issue in result["issues"]
    }:
        raise CorruptInput("MISSING_GLOBAL_DIAGNOSTIC")
    unit_index = {unit["unit_id"]: unit for unit in prepared.partition_plan["units"]}
    units = [unit_index[identity] for identity in partition["unit_ids"]]
    source_groups, source_support = _source_groups(prepared, units)
    summaries = {}
    for group in result["groups"]:
        if (
            type(group) is not dict
            or group.keys() != _GROUP_FIELDS
            or type(group["matched"]) is not bool
        ):
            raise CorruptInput("INVALID_GROUP_RESULT")
        identifier = group["group_id"]
        if (
            type(identifier) is not str
            or identifier in summaries
            or identifier not in source_groups
        ):
            raise CorruptInput("GROUP_COVERAGE_MISMATCH")
        if {name: value for name, value in group.items() if name != "matched"} != source_groups[
            identifier
        ]:
            raise CorruptInput("GROUP_SUMMARY_MISMATCH")
        summaries[identifier] = group
    if summaries.keys() != source_groups.keys():
        raise CorruptInput("GROUP_COVERAGE_MISMATCH")
    assigned_keys = {
        prepared.statement["effects"][ordinal]["provider_key"]
        for unit in units
        for ordinal in unit["effect_ordinals"]
    }
    claims = result["matched_provider_keys"]
    if (
        any(type(key) is not str for key in claims)
        or claims != sorted(set(claims))
        or not set(claims) <= assigned_keys
    ):
        raise CorruptInput("MATCH_COVERAGE_MISMATCH")
    bad = set()
    for issue in (*prepared.global_issues, *result["issues"]):
        if issue["category"] != "TIMING_ONLY":
            bad.add(issue["object_id"])
            bad.update(prepared.evidence_keys.get(issue["object_id"], ()))
    for key in claims:
        if key in bad or not _exact_key(prepared, key):
            raise CorruptInput("UNSUPPORTED_EXACT_MATCH")
    for group in summaries.values():
        if group["matched"] and (
            not source_support[group["group_id"]]
            or not set(group["provider_keys"]) <= set(claims)
            or (set(group["provider_keys"]) | set(group["row_ids"])) & bad
        ):
            raise CorruptInput("UNSUPPORTED_GROUP_MATCH")
