"""Literal synthetic financial fixtures; no production posting/money helpers.

One unit: opening100, purchase80, payout60+2; clearing38/customer20/merchant18.
The historical local observation fixture is not an external authentication oracle.
The statement is independently HMAC-signed from the literal60/2/62 facts below.
"""

import hashlib
import hmac
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

SECRET = bytes.fromhex("42" * 32)
NAMESPACE = UUID("4a000000-0000-4000-8000-000000000001")
WORKSPACE = "10000000-0000-4000-8000-000000000001"
PROVIDER = "70000000-0000-4000-8000-000000000002"
CLEARING = "30000000-0000-4000-8000-000000000001"
STAMP = "2026-10-29T10:00:00Z"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def identity(name):
    return str(uuid5(NAMESPACE, name))


def replace_ids(value, mapping):
    if isinstance(value, dict):
        return {key: replace_ids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_ids(item, mapping) for item in value]
    if not isinstance(value, str):
        return value
    for old, new in mapping.items():
        value = value.replace(old, new)
    return value


def workload(count, *, skew=False):
    if type(count) is not int or not 1 <= count <= 1600:
        raise ValueError("workload count must be1–1600")
    template = json.loads(Path(__file__).with_name("template_v7.json").read_text())
    result = {
        key: [] if isinstance(value, list) else deepcopy(value) for key, value in template.items()
    }
    result["schema_versions"] = list(template["schema_versions"])
    ids = set()

    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            try:
                if str(UUID(value)) == value:
                    ids.add(value)
            except ValueError:
                pass

    collect(template)
    common = {WORKSPACE, PROVIDER, CLEARING}
    if skew:
        common.update(row["id"] for row in template["accounts"])
        common.update(row["owner_id"] for row in template["accounts"])
    accounts, balances, effects, rows = {}, {}, [], []
    members = []
    for i in range(count):
        mapping = {old: identity(f"{i}:{old}") for old in sorted(ids - common)}
        unit = replace_ids(template, mapping)
        for table in ("operations", "requests"):
            for row in unit[table]:
                raw = replace_ids(bytes.fromhex(row["payload_hex"]).decode(), mapping).encode()
                row["payload_hex"], row["payload_sha256"] = (
                    raw.hex(),
                    hashlib.sha256(raw).hexdigest(),
                )
                if table == "requests":
                    row["key"] = f"benchmark-{i}-{row['route_family']}"
        payout = unit["payouts"][0]
        raw = replace_ids(bytes.fromhex(payout["provider_request_hex"]).decode(), mapping).encode()
        payout["provider_request_hex"] = raw.hex()
        payout["provider_request_sha256"] = hashlib.sha256(raw).hexdigest()
        payout["source_sequence"] = i + 1
        observation = unit["provider_observations"][0]
        payload = json.loads(observation["signed_payload"])
        payload["request"] = json.loads(raw)
        payload["request_sha256"] = hashlib.sha256(raw).hexdigest()
        payload["source_sequence"] = i + 1
        signed = canonical(payload)
        observation["signed_payload"] = signed.decode()
        observation["source_sequence"] = i + 1
        envelope = canonical(
            {
                "key_id": observation["key_id"],
                "payload": signed.decode(),
                "signature": observation["signature"],
            }
        )
        observation["envelope_hex"] = envelope.hex()
        observation["envelope_sha256"] = hashlib.sha256(envelope).hexdigest()
        for row in unit["accounts"]:
            accounts[row["id"]] = row
        for row in unit["balances"]:
            if row["account_id"] not in balances:
                balances[row["account_id"]] = deepcopy(row)
            else:
                combined = balances[row["account_id"]]
                for field in ("debits", "credits", "reserved", "booked"):
                    combined[field] = str(int(combined[field]) + int(row[field]))
                combined["version"] += row["version"]
        for key, value in unit.items():
            if isinstance(value, list) and key not in {"accounts", "balances", "schema_versions"}:
                result[key].extend(value)
        key = payout["provider_key"]
        row_id = identity("skew-row" if skew else f"row-{i}")
        batch = identity("skew-batch" if skew else f"batch-{i}")
        group = {
            "namespace": PROVIDER,
            "batch_id": batch,
            "currency": "PLN",
            "merchant_id": payout["merchant_account_id"],
            "fee_rule_version": "quoted_fee_v1",
        }
        effects.append(
            {
                "provider_key": key,
                "execution_sequence": i + 1,
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "request": json.loads(raw),
                "assignment": {
                    "sequence": i + 1,
                    "batch_id": batch,
                    "row_id": row_id,
                    "effective_at": STAMP,
                    "recorded_at": STAMP,
                    "fee_rule_version": "quoted_fee_v1",
                },
            }
        )
        if skew:
            members.append(key)
        else:
            rows.append(
                {
                    "row_id": row_id,
                    "group": group,
                    "provider_keys": [key],
                    "principal_minor": "60",
                    "fee_minor": "2",
                    "gross_minor": "62",
                }
            )
    if skew:
        rows.append(
            {
                "row_id": row_id,
                "group": group,
                "provider_keys": sorted(members),
                "principal_minor": str(60 * count),
                "fee_minor": str(2 * count),
                "gross_minor": str(62 * count),
            }
        )
    result["accounts"], result["balances"] = list(accounts.values()), list(balances.values())
    sorts = {
        "accounts": ("id",),
        "balances": ("account_id",),
        "operations": ("operation_id",),
        "requests": ("client_id", "route_family", "key"),
        "journals": ("journal_id",),
        "entries": ("journal_id", "line_no"),
        "payouts": ("operation_id",),
        "reservations": ("payout_id",),
        "reservation_events": ("payout_id", "event_no"),
        "dispatch_intents": ("payout_id",),
        "provider_observations": ("observation_id",),
        "observation_receipts": ("receipt_id",),
        "payout_resolutions": ("payout_id",),
    }
    for name, fields in sorts.items():
        result[name].sort(key=lambda row: tuple(row[field] for field in fields))
    statement = {
        "kind": "settlement_statement",
        "issuer": "ledgerops-provider-sim",
        "schema_version": 1,
        "scope": "COMPLETE_WORKSPACE_EFFECTS_AT_WATERMARK",
        "time_basis": "SETTLEMENT_EFFECTIVE_TIME",
        "namespace": PROVIDER,
        "workspace_id": WORKSPACE,
        "business_date": "2026-10-29",
        "timezone": "Europe/Warsaw",
        "start_inclusive": "2026-10-28T23:00:00Z",
        "end_exclusive": "2026-10-29T23:00:00Z",
        "version": 1,
        "generated_at": STAMP,
        "effect_watermark": count,
        "assignment_watermark": count,
        "fee_rule_version": "quoted_fee_v1",
        "rows": sorted(rows, key=lambda r: r["row_id"]),
        "effects": sorted(effects, key=lambda r: r["provider_key"]),
    }
    payload = canonical(statement)
    signature = hmac.new(
        SECRET, b"ledgerops.provider-statement:v1\nbench-v1\n" + payload, hashlib.sha256
    ).hexdigest()
    return result, canonical(
        {"key_id": "bench-v1", "payload": payload.decode(), "signature": signature}
    )
