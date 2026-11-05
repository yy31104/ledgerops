"""Exact identity joins and complete explicit groups; never infer payment by total."""

import hashlib
from collections import Counter, defaultdict

from .inputs import inspect_internal, payout_key
from .statement import FEE_RULE, canonical, instant, provider_key

CATEGORIES = (
    "CORRUPT_INPUT",
    "INPUT_CONTRADICTION",
    "DUPLICATE_EFFECT",
    "AMBIGUOUS_GROUP",
    "WRONG_OWNER",
    "CURRENCY_MISMATCH",
    "WRONG_AMOUNT",
    "FEE_MISMATCH",
    "MISSING_INTERNAL",
    "UNEXPECTED_INTERNAL",
    "MISSING_PROVIDER",
    "UNEXPECTED_PROVIDER",
    "TIMING_ONLY",
)


class Issues:
    def __init__(self, existing=()):
        self.rows = {}
        for row in existing:
            self.rows[canonical(row)] = row

    def add(self, category, object_id, field, expected, actual, *, scope="INVENTORY", refs=()):
        if category not in CATEGORIES:
            raise ValueError("unknown reconciliation category")
        row = dict(
            category=category,
            object_id=object_id,
            field=field,
            expected=expected,
            actual=actual,
            scope=scope,
            refs=sorted(refs),
        )
        self.rows[canonical(row)] = row

    def sorted(self):
        order = {value: index for index, value in enumerate(CATEGORIES)}
        return sorted(
            self.rows.values(),
            key=lambda row: (
                order[row["category"]],
                row["object_id"],
                row["field"],
                canonical(row),
            ),
        )


def group_key(group):
    if group is None or any(
        group[name] is None for name in ("namespace", "batch_id", "merchant_id")
    ):
        return None
    return tuple(
        group[name]
        for name in ("namespace", "batch_id", "currency", "merchant_id", "fee_rule_version")
    )


def effect_group(effect, namespace):
    assignment = effect["assignment"]
    if assignment is None or assignment["batch_id"] is None:
        return None
    request = effect["request"]
    return (
        namespace,
        assignment["batch_id"],
        request["currency"],
        request["merchant_id"],
        assignment["fee_rule_version"],
    )


def in_window(value, statement):
    return (
        instant(statement["start_inclusive"])
        <= instant(value)
        < instant(statement["end_exclusive"])
    )


def inspect_sources(statement, issues):
    """Global semantics run before any partition; duplicate physical rows stay in the plan."""
    effects, rows, allocations = defaultdict(list), defaultdict(list), defaultdict(list)
    execution_ids, assignment_ids, batches = defaultdict(list), defaultdict(list), defaultdict(set)
    for effect in statement["effects"]:
        key, request = effect["provider_key"], effect["request"]
        effects[key].append(effect)
        execution_ids[effect["execution_sequence"]].append(key)
        expected_identity = (
            statement["namespace"],
            statement["workspace_id"],
            request["operation_id"],
        )
        if (
            provider_key(key) != expected_identity
            or request["workspace_id"] != statement["workspace_id"]
        ):
            issues.add(
                "INPUT_CONTRADICTION",
                key,
                "provider_identity",
                list(expected_identity),
                list(provider_key(key)),
            )
        digest = hashlib.sha256(canonical(request)).hexdigest()
        if digest != effect["request_sha256"]:
            issues.add(
                "INPUT_CONTRADICTION", key, "request_sha256", digest, effect["request_sha256"]
            )
        if int(request["principal_minor"]) + int(request["fee_minor"]) != int(
            request["gross_minor"]
        ):
            issues.add(
                "INPUT_CONTRADICTION",
                key,
                "gross_minor",
                str(int(request["principal_minor"]) + int(request["fee_minor"])),
                request["gross_minor"],
            )
        if effect["execution_sequence"] > statement["effect_watermark"]:
            issues.add(
                "INPUT_CONTRADICTION",
                key,
                "execution_sequence",
                f"<= {statement['effect_watermark']}",
                effect["execution_sequence"],
            )
        assignment = effect["assignment"]
        if assignment is not None:
            if assignment["batch_id"] is not None:
                batches[assignment["batch_id"]].add(
                    (
                        assignment["effective_at"],
                        assignment["recorded_at"],
                        assignment["fee_rule_version"],
                    )
                )
            assignment_ids[assignment["sequence"]].append(key)
            if assignment["sequence"] > statement["assignment_watermark"]:
                issues.add(
                    "INPUT_CONTRADICTION",
                    key,
                    "assignment_sequence",
                    f"<= {statement['assignment_watermark']}",
                    assignment["sequence"],
                )
            if assignment["batch_id"] is None or assignment["row_id"] is None:
                issues.add("AMBIGUOUS_GROUP", key, "assignment", "explicit batch and row", None)
            if assignment["fee_rule_version"] != FEE_RULE:
                issues.add(
                    "FEE_MISMATCH",
                    key,
                    "fee_rule_version",
                    FEE_RULE,
                    assignment["fee_rule_version"],
                )
    for batch, variants in batches.items():
        if len(variants) > 1:
            issues.add(
                "INPUT_CONTRADICTION",
                batch,
                "immutable_batch_metadata",
                "one version",
                [list(value) for value in sorted(variants)],
                scope="SETTLEMENT_WINDOW",
            )
    for key, duplicates in effects.items():
        if len(duplicates) > 1:
            issues.add("DUPLICATE_EFFECT", key, "provider_effect_count", 1, len(duplicates))
    for field, sequence_map in (
        ("execution_sequence", execution_ids),
        ("assignment_sequence", assignment_ids),
    ):
        for sequence, keys in sequence_map.items():
            if len(keys) > 1:
                issues.add(
                    "INPUT_CONTRADICTION", str(sequence), field, "one source record", sorted(keys)
                )
    for row in statement["rows"]:
        rows[row["row_id"]].append(row)
        if group_key(row["group"]) is None or not row["provider_keys"]:
            issues.add(
                "AMBIGUOUS_GROUP",
                row["row_id"],
                "members/group",
                "explicit nonempty group and membership",
                None,
                scope="SETTLEMENT_WINDOW",
            )
        for key in row["provider_keys"] or ():
            allocations[key].append(row["row_id"])
    for identifier, duplicate in rows.items():
        if len(duplicate) > 1:
            issues.add(
                "INPUT_CONTRADICTION",
                identifier,
                "statement_row_count",
                1,
                len(duplicate),
                scope="SETTLEMENT_WINDOW",
            )
    for key, owners in allocations.items():
        if len(owners) > 1:
            issues.add(
                "AMBIGUOUS_GROUP",
                key,
                "member_allocation",
                "one row",
                sorted(owners),
                scope="SETTLEMENT_WINDOW",
            )
    for key, same_key in effects.items():
        for effect in same_key:
            assignment = effect["assignment"]
            expected_rows = []
            if assignment is not None and in_window(assignment["effective_at"], statement):
                expected_rows = [assignment["row_id"]]
            actual_rows = allocations.get(key, [])
            if sorted(actual_rows) != expected_rows:
                issues.add(
                    "INPUT_CONTRADICTION",
                    key,
                    "settlement_coverage",
                    expected_rows,
                    sorted(actual_rows),
                    scope="SETTLEMENT_WINDOW",
                )
    if statement["fee_rule_version"] != FEE_RULE:
        issues.add(
            "FEE_MISMATCH", "statement", "fee_rule_version", FEE_RULE, statement["fee_rule_version"]
        )
    return dict(effects)


def _compare_request(intent, request, identifier, issues):
    """Ownership precedes amounts; coincidental equal totals never establish a match."""
    agrees = True
    for local, remote in (
        ("merchant_account_id", "merchant_id"),
        ("beneficiary_id", "beneficiary_id"),
    ):
        if request[remote] != intent[local]:
            issues.add("WRONG_OWNER", identifier, remote, intent[local], request[remote])
            agrees = False
    if not agrees:
        return False
    if request["currency"] != intent["currency"]:
        issues.add(
            "CURRENCY_MISMATCH", identifier, "currency", intent["currency"], request["currency"]
        )
        return False
    for name, category in (("principal_minor", "WRONG_AMOUNT"), ("fee_minor", "FEE_MISMATCH")):
        if request[name] != intent[name]:
            issues.add(category, identifier, name, intent[name], request[name])
            agrees = False
    gross = str(int(intent["principal_minor"]) + int(intent["fee_minor"]))
    if request["gross_minor"] != gross:
        issues.add("WRONG_AMOUNT", identifier, "gross_minor", gross, request["gross_minor"])
        agrees = False
    return agrees


def _payout_projection(facts, identifier, issues):
    intent, row = facts.intents[identifier], facts.payouts.get(identifier)
    if row is None:
        issues.add("INPUT_CONTRADICTION", identifier, "payout_record", "present", None)
        return
    request = {**row, "merchant_id": row["merchant_account_id"]}
    _compare_request(intent, request, identifier, issues)
    for field, expected in (
        ("provider_namespace", intent["provider_namespace"]),
        ("provider_key", payout_key(facts, identifier)),
    ):
        if row[field] != expected:
            issues.add("INPUT_CONTRADICTION", identifier, field, expected, row[field])


def match_effects(facts, statement, effect_index, unit, issues, global_bad):
    keys = {statement["effects"][index]["provider_key"] for index in unit["effect_ordinals"]}
    correct = inspect_internal(facts, set(effect_index), issues, identifiers=unit["operation_ids"])
    matches = []
    for identifier in unit["operation_ids"]:
        operation, intent = facts.operations.get(identifier), facts.intents.get(identifier)
        if operation is None or operation["kind"] != "PAYOUT" or intent is None:
            continue
        if intent["provider_namespace"] != statement["namespace"]:
            continue  # This one-namespace statement makes no claims about other providers.
        if operation["admission"] == "ACCEPTED":
            _payout_projection(facts, identifier, issues)
            if facts.journals.get(identifier) and payout_key(facts, identifier) not in effect_index:
                issues.add(
                    "MISSING_PROVIDER",
                    identifier,
                    "complete_effect_inventory",
                    payout_key(facts, identifier),
                    None,
                )
    for key in sorted(keys):
        same_key = effect_index[key]
        if len(same_key) != 1:
            continue
        effect = same_key[0]
        identifier = provider_key(key)[2]
        operation, intent = facts.operations.get(identifier), facts.intents.get(identifier)
        if (
            operation is None
            or operation["kind"] != "PAYOUT"
            or intent is None
            or operation["admission"] != "ACCEPTED"
            or intent["provider_namespace"] != statement["namespace"]
        ):
            issues.add(
                "UNEXPECTED_PROVIDER",
                key,
                "accepted_payout_intent",
                "present",
                "absent or rejected",
            )
            continue
        agrees = _compare_request(intent, effect["request"], key, issues)
        payout = facts.payouts.get(identifier)
        if payout is not None:
            local_deadline = instant(payout["dispatch_deadline"])
            if local_deadline != instant(effect["request"]["dispatch_deadline"]):
                issues.add(
                    "INPUT_CONTRADICTION",
                    key,
                    "dispatch_deadline",
                    local_deadline.isoformat(),
                    effect["request"]["dispatch_deadline"],
                )
                agrees = False
        resolution = facts.resolutions.get(identifier)
        if resolution is not None and resolution["state"] != "SUCCEEDED":
            issues.add(
                "INPUT_CONTRADICTION",
                key,
                "provider_success/local_resolution",
                "SUCCEEDED",
                resolution["state"],
            )
            agrees = False
        if (
            agrees
            and identifier in correct
            and key not in global_bad
            and identifier not in global_bad
        ):
            matches.append(key)
            assignment = effect["assignment"]
            if assignment is not None:
                recorded = facts.journals[identifier][0]["recorded_at"]
                if in_window(recorded, statement) != in_window(
                    assignment["effective_at"], statement
                ):
                    issues.add(
                        "TIMING_ONLY",
                        key,
                        "posting_vs_settlement_window",
                        recorded,
                        assignment["effective_at"],
                        scope="SETTLEMENT_WINDOW",
                        refs=(
                            "ledger:journal:" + facts.journals[identifier][0]["journal_id"],
                            "statement:row:" + str(assignment["row_id"]),
                        ),
                    )
    return matches


def match_groups(statement, effect_index, unit, issues, bad_ids):
    grouped = defaultdict(list)
    for index in unit["row_ordinals"]:
        row = statement["rows"][index]
        key = group_key(row["group"])
        if key is not None:
            grouped[key].append(row)
    result = []
    for key, rows in sorted(grouped.items()):
        initial = len(issues.rows)
        totals = Counter()
        all_keys, row_ids = [], []
        for row in rows:
            row_ids.append(row["row_id"])
            members = row["provider_keys"] or []
            all_keys.extend(members)
            summed = Counter()
            complete = bool(members)
            for member in members:
                effects = effect_index.get(member, [])
                if len(effects) != 1:
                    issues.add(
                        "INPUT_CONTRADICTION",
                        row["row_id"],
                        "member_effect",
                        member,
                        len(effects),
                        scope="SETTLEMENT_WINDOW",
                    )
                    complete = False
                    continue
                effect = effects[0]
                actual = effect_group(effect, statement["namespace"])
                if actual is None:
                    issues.add(
                        "AMBIGUOUS_GROUP",
                        member,
                        "assignment",
                        "explicit group",
                        None,
                        scope="SETTLEMENT_WINDOW",
                    )
                    complete = False
                elif actual != key:
                    category = (
                        "WRONG_OWNER"
                        if actual[3] != key[3]
                        else (
                            "CURRENCY_MISMATCH"
                            if actual[2] != key[2]
                            else ("FEE_MISMATCH" if actual[4] != key[4] else "INPUT_CONTRADICTION")
                        )
                    )
                    issues.add(
                        category,
                        row["row_id"],
                        "member_group",
                        list(actual),
                        list(key),
                        scope="SETTLEMENT_WINDOW",
                    )
                assignment = effect["assignment"]
                if assignment is not None and assignment["row_id"] != row["row_id"]:
                    issues.add(
                        "INPUT_CONTRADICTION",
                        member,
                        "assigned_row",
                        assignment["row_id"],
                        row["row_id"],
                        scope="SETTLEMENT_WINDOW",
                    )
                for name in ("principal_minor", "fee_minor", "gross_minor"):
                    summed[name] += int(effect["request"][name])
            if complete:
                for name in ("principal_minor", "fee_minor", "gross_minor"):
                    if str(summed[name]) != row[name]:
                        issues.add(
                            "INPUT_CONTRADICTION",
                            row["row_id"],
                            name,
                            str(summed[name]),
                            row[name],
                            scope="SETTLEMENT_WINDOW",
                        )
                if row["fee_minor"] != str(summed["fee_minor"]):
                    issues.add(
                        "FEE_MISMATCH",
                        row["row_id"],
                        "fee_minor",
                        str(summed["fee_minor"]),
                        row["fee_minor"],
                        scope="SETTLEMENT_WINDOW",
                    )
            for name in ("principal_minor", "fee_minor", "gross_minor"):
                totals[name] += int(row[name])
        if key[4] != FEE_RULE:
            issues.add(
                "FEE_MISMATCH",
                "group:" + key[1],
                "fee_rule_version",
                FEE_RULE,
                key[4],
                scope="SETTLEMENT_WINDOW",
            )
        result.append(
            dict(
                group_id=hashlib.sha256(canonical(list(key))).hexdigest(),
                group_key=list(key),
                row_ids=sorted(row_ids),
                provider_keys=sorted(all_keys),
                **{
                    name: str(totals[name])
                    for name in ("principal_minor", "fee_minor", "gross_minor")
                },
                matched=(
                    len(issues.rows) == initial and not (set(row_ids) | set(all_keys)) & bad_ids
                ),
            )
        )
    return result
