"""Pinned, deterministic settlement reconciliation; reports never change the ledger."""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

from ledgerops.export import ExportError, LoadedExport, load_export_bundle
from ledgerops.verification import verify_snapshot

from .inputs import collect_ledger, payout_key
from .planning import build_plan
from .results import validate_partition_result
from .rules import CATEGORIES, Issues, inspect_sources, match_effects, match_groups
from .statement import (
    CorruptInput,
    canonical,
    decode,
    parse_statement,
)
from .statement import (
    StatementTrust as StatementTrust,
)

RULE_VERSION = "ledgerops.reconciliation.v1"
PARSER_VERSION = 1


def semantic_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _reference(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


@dataclass(frozen=True)
class PreparedInputs:
    """Authenticated pinned facts and a fixed partition plan, containing no secret keys."""

    input_manifest: dict
    partition_plan: dict
    facts: object
    statement: dict
    effects: dict
    evidence_keys: dict
    global_issues: tuple
    internal_verification: dict


def prepare_inputs(bundle, statement_bytes, *, trust, code_revision, dirty=False, partitions=16):
    """Global validation precedes work assignment; worker count never alters this plan."""
    if type(bundle) is not LoadedExport:
        raise CorruptInput("PINNED_LEDGER_EXPORT_REQUIRED")
    if type(code_revision) is not str or not re.fullmatch(r"[0-9a-f]{40}", code_revision):
        raise CorruptInput("MATCHER_CODE_REVISION_REQUIRED")
    if type(dirty) is not bool:
        raise CorruptInput("INVALID_DIRTY_FLAG")
    snapshot, manifest = bundle.snapshot, bundle.manifest
    statement = parse_statement(statement_bytes, trust=trust)
    if snapshot["workspace"]["id"] != statement["workspace_id"]:
        raise CorruptInput("INPUT_WORKSPACE_MISMATCH")
    issues = Issues()
    facts = collect_ledger(snapshot, issues)
    effects = inspect_sources(statement, issues)
    internal = verify_snapshot(snapshot).as_dict()
    # Internal checks are reported alongside matching, never a gate hiding a
    # concrete WRONG_OWNER/WRONG_AMOUNT behind a generic invalid-input result.
    bindings = {
        "manifest_version": 1,
        "inputs": {
            "ledger_manifest": _reference(bundle.manifest_bytes),
            "ledger_data": _reference(bundle.data_bytes),
            "statement": _reference(statement_bytes),
        },
        "workspace_id": facts.workspace,
        "provider_namespace": statement["namespace"],
        "business_date": statement["business_date"],
        "timezone": statement["timezone"],
        "start_inclusive": statement["start_inclusive"],
        "end_exclusive": statement["end_exclusive"],
        "statement_version": statement["version"],
        "statement_key_id": decode(statement_bytes)["key_id"],
        "effect_watermark": statement["effect_watermark"],
        "assignment_watermark": statement["assignment_watermark"],
        "export_id": manifest["export_id"],
        "ledger_source_code_revision": manifest["code_revision"],
        "ledger_source_dirty": manifest["dirty"],
        "rule_version": RULE_VERSION,
        "parser_version": PARSER_VERSION,
        "code_revision": code_revision,
        "dirty": dirty,
    }
    try:
        plan = build_plan(
            snapshot,
            statement,
            ledger_sha256=bindings["inputs"]["ledger_data"]["sha256"],
            statement_sha256=bindings["inputs"]["statement"]["sha256"],
            manifest_sha256=bindings["inputs"]["ledger_manifest"]["sha256"],
            partitions=partitions,
        )
    except ValueError as exc:
        raise CorruptInput("INVALID_PARTITION_PLAN") from exc
    evidence_keys = defaultdict(set)
    for effect in statement["effects"]:
        key = effect["provider_key"]
        evidence_keys[effect["request"]["operation_id"]].add(key)
        evidence_keys[str(effect["execution_sequence"])].add(key)
        evidence_keys["statement"].add(key)
        assignment = effect["assignment"]
        if assignment is not None:
            for identity in (
                assignment["batch_id"],
                assignment["row_id"],
                str(assignment["sequence"]),
            ):
                if identity is not None:
                    evidence_keys[identity].add(key)
    for row in statement["rows"]:
        evidence_keys[row["row_id"]].update(row["provider_keys"] or ())
    return PreparedInputs(
        bindings,
        plan,
        facts,
        statement,
        effects,
        {key: tuple(sorted(values)) for key, values in evidence_keys.items()},
        tuple(issues.sorted()),
        internal,
    )


def _bad_objects(issues, facts, evidence_keys):
    bad = {row["object_id"] for row in issues if row["category"] != "TIMING_ONLY"}
    for identifier in tuple(bad):
        bad.update(evidence_keys.get(identifier, ()))
        if identifier in facts.intents and facts.operations[identifier]["kind"] == "PAYOUT":
            bad.add(payout_key(facts, identifier))
    return bad


def match_partition(prepared, partition_id):
    """Compute complete units; duplicate business identities are never coverage identities."""
    if type(prepared) is not PreparedInputs:
        raise CorruptInput("PREPARED_INPUTS_REQUIRED")
    selected = [
        part
        for part in prepared.partition_plan["partitions"]
        if part["partition_id"] == partition_id
    ]
    if len(selected) != 1 or type(partition_id) is not int:
        raise CorruptInput("UNKNOWN_PARTITION")
    partition = selected[0]
    units = {unit["unit_id"]: unit for unit in prepared.partition_plan["units"]}
    issues, groups, matches = Issues(), [], []
    global_bad = _bad_objects(prepared.global_issues, prepared.facts, prepared.evidence_keys)
    has_global = False
    for unit_id in partition["unit_ids"]:
        unit = units[unit_id]
        global_metadata = (
            prepared.input_manifest["inputs"]["ledger_data"]["sha256"] + ":ledger_metadata:0"
        )
        if global_metadata in unit["coverage"]:
            has_global = True
            for issue in prepared.global_issues:
                issues.rows[canonical(issue)] = issue
        local = Issues()
        matched = match_effects(
            prepared.facts, prepared.statement, prepared.effects, unit, local, global_bad
        )
        bad = global_bad | _bad_objects(local.sorted(), prepared.facts, prepared.evidence_keys)
        groups.extend(match_groups(prepared.statement, prepared.effects, unit, local, bad))
        # A group or source contradiction also prevents its member from being
        # advertised as an exact match, even when the single amount agreed.
        bad |= _bad_objects(local.sorted(), prepared.facts, prepared.evidence_keys)
        matches.extend(key for key in matched if key not in bad)
        for issue in local.sorted():
            issues.rows[canonical(issue)] = issue
    return {
        "partition_id": partition_id,
        "coverage": list(partition["coverage"]),
        "result": {
            "issues": issues.sorted(),
            "groups": sorted(groups, key=lambda group: group["group_id"]),
            "matched_provider_keys": sorted(matches),
            "global_checks": has_global,
        },
    }


def merge_partition_results(prepared, results):
    """Reject missing/duplicate/wrong coverage before constructing the stable report."""
    expected = {part["partition_id"]: part for part in prepared.partition_plan["partitions"]}
    if type(results) is not list or len(results) != len(expected):
        raise CorruptInput("INCOMPLETE_PARTITION_RESULTS")
    issues, groups, matches, seen, coverage = Issues(), [], [], set(), []
    globals_seen = 0
    for item in results:
        if type(item) is not dict or item.keys() != {"partition_id", "coverage", "result"}:
            raise CorruptInput("INVALID_PARTITION_RESULT")
        identity = item["partition_id"]
        if type(identity) is not int or identity in seen or identity not in expected:
            raise CorruptInput("DUPLICATE_OR_UNKNOWN_PARTITION")
        seen.add(identity)
        if item["coverage"] != expected[identity]["coverage"]:
            raise CorruptInput("PARTITION_COVERAGE_MISMATCH")
        coverage.extend(item["coverage"])
        result = item["result"]
        validate_partition_result(prepared, expected[identity], result)
        globals_seen += result["global_checks"]
        for issue in result["issues"]:
            if issue["category"] not in CATEGORIES:
                raise CorruptInput("UNKNOWN_DIFFERENCE_CATEGORY")
            issues.rows[canonical(issue)] = issue
        groups.extend(result["groups"])
        matches.extend(result["matched_provider_keys"])
    if sorted(coverage) != prepared.partition_plan["expected_coverage"] or globals_seen != 1:
        raise CorruptInput("GLOBAL_COVERAGE_MISMATCH")
    if len({group["group_id"] for group in groups}) != len(groups) or len(set(matches)) != len(
        matches
    ):
        raise CorruptInput("SPLIT_GROUP_OR_DUPLICATE_MATCH")
    ordered = issues.sorted()
    internal = prepared.internal_verification
    workspace_unresolved = internal.get("external_evidence", {}).get("unresolved_payout_ids", [])
    manifest = prepared.input_manifest
    unresolved = [
        identifier
        for identifier in workspace_unresolved
        if prepared.facts.intents.get(identifier, {}).get(
            "provider_namespace",
            prepared.facts.payouts.get(identifier, {}).get("provider_namespace"),
        )
        == manifest["provider_namespace"]
    ]
    unsettled = sorted(
        {
            effect["provider_key"]
            for effect in prepared.statement["effects"]
            if effect["assignment"] is None
        }
    )
    settlement_complete = not unsettled and not any(
        issue["scope"] == "SETTLEMENT_WINDOW" and issue["category"] != "TIMING_ONLY"
        for issue in ordered
    )
    required_matches = set(prepared.effects)
    for identifier, resolution in prepared.facts.resolutions.items():
        intent = prepared.facts.intents.get(identifier)
        if (
            resolution["state"] == "SUCCEEDED"
            and intent is not None
            and intent.get("provider_namespace") == manifest["provider_namespace"]
        ):
            required_matches.add(payout_key(prepared.facts, identifier))
    return {
        "report_version": 1,
        "status": "COMPLETE",
        "usable": True,
        "reconciled": set(matches) == required_matches
        and all(group["matched"] for group in groups)
        and not ordered
        and internal["healthy"]
        and not unresolved
        and settlement_complete,
        "rule_version": RULE_VERSION,
        "parser_version": PARSER_VERSION,
        "workspace_id": manifest["workspace_id"],
        "provider_namespace": manifest["provider_namespace"],
        "business_date": manifest["business_date"],
        "timezone": manifest["timezone"],
        "window": {
            "start_inclusive": manifest["start_inclusive"],
            "end_exclusive": manifest["end_exclusive"],
        },
        "statement_version": manifest["statement_version"],
        "inputs": manifest,
        "inventory_scope": "COMPLETE_PINNED_WORKSPACE_INPUTS",
        "settlement_scope": "EXPLICIT_SELECTED_BUSINESS_DAY",
        "issues": ordered,
        "groups": sorted(groups, key=lambda group: group["group_id"]),
        "matched_provider_keys": sorted(matches),
        "unmatched_provider_keys": sorted(required_matches - set(matches)),
        "unresolved_payout_ids": sorted(unresolved),
        "workspace_unresolved_payout_ids": sorted(workspace_unresolved),
        "unsettled_provider_keys": unsettled,
        "settlement_complete": settlement_complete,
        "internal_verification": internal,
        "limits": [
            "HMAC proves source/key agreement, not completeness or truth of the provider database.",
            "Stored requests are intent evidence, not proof of correct user-request capture.",
            "Ledger event signatures are not authenticated by the independent A/B verifier.",
            "Missing rows describe these fixed inputs; source watermarks are not a shared clock.",
            "Unassigned effects have no known settlement day at the fixed assignment watermark.",
            "This report cannot authorize a release, financial correction or external payment.",
        ],
    }


def _corrupt(code):
    issues = Issues()
    issues.add("CORRUPT_INPUT", "inputs", "validation", "supported authenticated inputs", code)
    return {
        "report_version": 1,
        "status": "CORRUPT_INPUT",
        "usable": False,
        "reconciled": False,
        "rule_version": RULE_VERSION,
        "parser_version": PARSER_VERSION,
        "issues": issues.sorted(),
        "groups": [],
        "matched_provider_keys": [],
    }


def reconcile_inputs(bundle, statement_bytes, *, trust, code_revision, dirty=False, partitions=16):
    try:
        prepared = prepare_inputs(
            bundle,
            statement_bytes,
            trust=trust,
            code_revision=code_revision,
            dirty=dirty,
            partitions=partitions,
        )
        results = [
            match_partition(prepared, part["partition_id"])
            for part in prepared.partition_plan["partitions"]
        ]
        return merge_partition_results(prepared, results)
    except CorruptInput as exc:
        return _corrupt(str(exc))


def reconcile_bundle(path, statement_bytes, *, trust, code_revision, dirty=False, partitions=16):
    try:
        bundle = load_export_bundle(path)
    except ExportError:
        return _corrupt("INVALID_LEDGER_EXPORT")
    return reconcile_inputs(
        bundle,
        statement_bytes,
        trust=trust,
        code_revision=code_revision,
        dirty=dirty,
        partitions=partitions,
    )
