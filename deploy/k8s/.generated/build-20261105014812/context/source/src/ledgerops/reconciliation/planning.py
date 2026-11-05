"""Deterministic whole-group planning with one owner for every physical input row.

This is a coverage algorithm, not a financial matcher. A duplicated key connecting
two groups merges them into one unit so that a worker can diagnose the conflict;
it never chooses a winner or discards the duplicated physical evidence.
"""

import hashlib
import json
import re
from collections import defaultdict

MAX_COVERAGE = 250_000
_METADATA = {"format_version", "workspace", "snapshot_witness", "schema_versions", "server_version"}
_GROUP_FIELDS = ("namespace", "batch_id", "currency", "merchant_id", "fee_rule_version")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class _Components:
    def __init__(self):
        self.parent = {}
        self.size = {}

    def add(self, label):
        self.parent.setdefault(label, label)
        self.size.setdefault(label, 1)
        return label

    def find(self, label):
        self.add(label)
        root = label
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[label] != root:
            parent = self.parent[label]
            self.parent[label] = root
            label = parent
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            if self.size[left] < self.size[right]:
                left, right = right, left
            self.parent[right] = left
            self.size[left] += self.size[right]


def _label(kind, identity):
    return kind + ":" + _canonical(identity)


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("INVALID_PLAN_INPUT_DIGEST")
    return value


def build_plan(
    snapshot, statement, *, ledger_sha256, statement_sha256, manifest_sha256, partitions=16
):
    """Plan validated input shapes without dropping semantically invalid source rows.

    Workers may read the complete pinned snapshot for reference data (for example
    the original purchase of a reversal). Ownership determines which unit emits
    findings, not permission to read those immutable reference facts.
    """
    if type(partitions) is not int or not 1 <= partitions <= 64:
        raise ValueError("INVALID_PARTITION_COUNT")
    for digest in (ledger_sha256, statement_sha256, manifest_sha256):
        _digest(digest)
    if type(snapshot) is not dict or type(statement) is not dict:
        raise ValueError("INVALID_PLAN_INPUT_SHAPE")
    tables = {}
    for name, rows in snapshot.items():
        if name in _METADATA:
            continue
        if (
            type(name) is not str
            or type(rows) is not list
            or any(type(row) is not dict for row in rows)
        ):
            raise ValueError("INVALID_PLAN_TABLE")
        tables[name] = rows
    effects, rows = statement.get("effects"), statement.get("rows")
    if (
        type(effects) is not list
        or type(rows) is not list
        or any(type(row) is not dict for row in effects + rows)
        or type(snapshot.get("workspace")) is not dict
    ):
        raise ValueError("INVALID_PLAN_INPUT_SHAPE")
    count = 4 + len(effects) + len(rows) + sum(len(table) for table in tables.values())
    if count > MAX_COVERAGE:
        raise ValueError("PLAN_COVERAGE_LIMIT")
    components = _Components()
    global_label = components.add("global")
    owners, operations, groups = {}, {}, {}
    effect_nodes, row_nodes = [], []

    def owner(digest, table, ordinal, node):
        identity = f"{digest}:{table}:{ordinal}"
        if identity in owners:
            raise ValueError("DUPLICATE_PLAN_PHYSICAL_IDENTITY")
        owners[identity] = components.add(node)

    def operation(identifier):
        node = components.add(_label("operation", identifier))
        operations[node] = identifier
        return node

    def provider_key(value):
        node = components.add(_label("provider_key", value))
        pieces = value.split(".") if type(value) is str else ()
        if len(pieces) == 4 and pieces[2] == snapshot["workspace"].get("id"):
            components.union(node, operation(pieces[3]))
        return node

    def group(value):
        if type(value) is not dict or any(
            type(value.get(field)) is not str for field in _GROUP_FIELDS
        ):
            return None
        identity = tuple(value[field] for field in _GROUP_FIELDS)
        node = components.add(_label("group", identity))
        groups[node] = identity
        return node

    owner(ledger_sha256, "ledger_metadata", 0, global_label)
    owner(ledger_sha256, "workspace", 0, global_label)
    owner(statement_sha256, "statement_metadata", 0, global_label)
    owner(manifest_sha256, "manifest_metadata", 0, global_label)

    # Journals get their own identity node first. This preserves contradictory
    # duplicate journal headers instead of overwriting a dict keyed by journal ID.
    for row in tables.get("journals", []):
        journal = components.add(_label("journal", row["journal_id"]))
        components.union(journal, operation(row["operation_id"]))

    for table, records in sorted(tables.items()):
        for ordinal, record in enumerate(records):
            nodes = []
            # A reversal owns its successful relation; the original purchase is
            # a read-only dependency and must not connect unrelated settlement units.
            if table == "reversal_effects":
                if record.get("reversal_operation_id") is not None:
                    nodes.append(operation(record["reversal_operation_id"]))
            else:
                for field in ("operation_id", "payout_id"):
                    if record.get(field) is not None:
                        nodes.append(operation(record[field]))
            if record.get("journal_id") is not None:
                nodes.append(components.add(_label("journal", record["journal_id"])))
            if table == "payouts" and record.get("provider_key") is not None:
                nodes.append(provider_key(record["provider_key"]))
            node = nodes[0] if nodes else global_label
            for other in nodes[1:]:
                components.union(node, other)
            owner(ledger_sha256, table, ordinal, node)

    for ordinal, effect in enumerate(effects):
        node = provider_key(effect["provider_key"])
        request = effect["request"]
        if request.get("workspace_id") == snapshot["workspace"].get("id"):
            components.union(node, operation(request["operation_id"]))
        assignment = effect.get("assignment")
        if assignment is not None:
            group_node = group(
                {
                    "namespace": statement["namespace"],
                    "batch_id": assignment.get("batch_id"),
                    "currency": request["currency"],
                    "merchant_id": request["merchant_id"],
                    "fee_rule_version": assignment["fee_rule_version"],
                }
            )
            if group_node is not None:
                components.union(node, group_node)
            if assignment.get("row_id") is not None:
                components.union(node, _label("row", assignment["row_id"]))
        owner(statement_sha256, "effects", ordinal, node)
        effect_nodes.append(node)

    for ordinal, row in enumerate(rows):
        node = components.add(_label("row", row["row_id"]))
        group_node = group(row.get("group"))
        if group_node is not None:
            components.union(node, group_node)
        for key in row.get("provider_keys") or ():
            components.union(node, provider_key(key))
        owner(statement_sha256, "rows", ordinal, node)
        row_nodes.append(node)

    labels_by_root = defaultdict(list)
    for node in components.parent:
        labels_by_root[components.find(node)].append(node)
    units = {}
    for root, labels in labels_by_root.items():
        unit_id = hashlib.sha256(_canonical(sorted(labels)).encode()).hexdigest()
        units[root] = {
            "unit_id": unit_id,
            "group_keys": [],
            "operation_ids": [],
            "effect_ordinals": [],
            "row_ordinals": [],
            "coverage": [],
        }
    for node, identifier in operations.items():
        units[components.find(node)]["operation_ids"].append(identifier)
    for node, identity in groups.items():
        units[components.find(node)]["group_keys"].append(list(identity))
    for ordinal, node in enumerate(effect_nodes):
        units[components.find(node)]["effect_ordinals"].append(ordinal)
    for ordinal, node in enumerate(row_nodes):
        units[components.find(node)]["row_ordinals"].append(ordinal)
    for identity, node in owners.items():
        units[components.find(node)]["coverage"].append(identity)
    assignments = [
        {"partition_id": index, "unit_ids": [], "coverage": []} for index in range(partitions)
    ]
    for unit in units.values():
        for field, value in unit.items():
            if field != "unit_id":
                value.sort()
        partition = assignments[int(unit["unit_id"], 16) % partitions]
        partition["unit_ids"].append(unit["unit_id"])
        partition["coverage"].extend(unit["coverage"])
    for partition in assignments:
        partition["unit_ids"].sort()
        partition["coverage"].sort()
    return {
        "plan_version": 1,
        "units": sorted(units.values(), key=lambda unit: unit["unit_id"]),
        "partitions": assignments,
        "expected_coverage": sorted(owners),
    }
