"""Hand-authored settlement facts, signatures and 13 difference-category goldens."""

import copy
import hashlib
import hmac
import json
from datetime import datetime

import pytest
from test_payout_external_verification import JOURNAL, finish, observe, v6
from test_payout_verification import BENEFICIARY, NAMESPACE, PAYOUT, REVISION, for_export
from test_verification import M1, M2, W
from test_verification import snapshot as snapshot

from ledgerops.export import (
    ExportError,
    LoadedExport,
    load_export,
    load_export_bundle,
    publish_snapshot,
    validate_export_bytes,
)
from ledgerops.reconciliation import (
    StatementTrust,
    match_partition,
    merge_partition_results,
    prepare_inputs,
    reconcile_bundle,
    reconcile_inputs,
    semantic_digest,
)
from ledgerops.reconciliation.statement import CorruptInput, day_bounds, parse_statement

SECRET = b"independent-reconciliation-test-secret32"
KEY_ID = "statement-test-v1"
BATCH = "a0000000-0000-4000-8000-000000000001"
ROW = "a0000000-0000-4000-8000-000000000002"
PROVIDER_KEY = f"v1.{NAMESPACE}.{W}.{PAYOUT}"
TRUST = StatementTrust(NAMESPACE, {KEY_ID: SECRET})


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def signed(statement):
    raw = encoded(statement)
    digest = hmac.new(
        SECRET, b"ledgerops.provider-statement:v1\n" + KEY_ID.encode() + b"\n" + raw, hashlib.sha256
    ).hexdigest()
    return encoded({"key_id": KEY_ID, "payload": raw.decode(), "signature": digest})


@pytest.fixture
def statement():
    request = {
        "workspace_id": W,
        "operation_id": PAYOUT,
        "merchant_id": M1,
        "beneficiary_id": BENEFICIARY,
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": "2026-10-29T10:01:00Z",
    }
    return {
        "kind": "settlement_statement",
        "issuer": "ledgerops-provider-sim",
        "schema_version": 1,
        "scope": "COMPLETE_WORKSPACE_EFFECTS_AT_WATERMARK",
        "time_basis": "SETTLEMENT_EFFECTIVE_TIME",
        "namespace": NAMESPACE,
        "workspace_id": W,
        "business_date": "2026-10-29",
        "timezone": "Europe/Warsaw",
        "start_inclusive": "2026-10-28T23:00:00Z",
        "end_exclusive": "2026-10-29T23:00:00Z",
        "version": 1,
        "generated_at": "2026-10-29T23:02:00Z",
        "effect_watermark": 1,
        "assignment_watermark": 1,
        "fee_rule_version": "quoted_fee_v1",
        "rows": [
            {
                "row_id": ROW,
                "group": {
                    "namespace": NAMESPACE,
                    "batch_id": BATCH,
                    "currency": "PLN",
                    "merchant_id": M1,
                    "fee_rule_version": "quoted_fee_v1",
                },
                "provider_keys": [PROVIDER_KEY],
                "principal_minor": "60",
                "fee_minor": "2",
                "gross_minor": "62",
            }
        ],
        "effects": [
            {
                "provider_key": PROVIDER_KEY,
                "execution_sequence": 1,
                "request_sha256": hashlib.sha256(encoded(request)).hexdigest(),
                "request": request,
                "assignment": {
                    "sequence": 1,
                    "batch_id": BATCH,
                    "row_id": ROW,
                    "effective_at": "2026-10-29T10:02:00Z",
                    "recorded_at": "2026-10-29T10:03:00Z",
                    "fee_rule_version": "quoted_fee_v1",
                },
            }
        ],
    }


@pytest.fixture
def data(snapshot):
    value = v6(copy.deepcopy(snapshot))
    finish(value, observe(value))
    return for_export(value)


def report(data, statement, tmp_path, *, raw=None, partitions=16):
    path = publish_snapshot(data, tmp_path, code_revision=REVISION)
    return reconcile_bundle(
        path,
        signed(statement) if raw is None else raw,
        trust=TRUST,
        code_revision=REVISION,
        partitions=partitions,
    )


def categories(result):
    return {row["category"] for row in result["issues"]}


def rehash(statement):
    for effect in statement["effects"]:
        effect["request_sha256"] = hashlib.sha256(encoded(effect["request"])).hexdigest()


def test_literal_success_reconciles_62_not_60_and_does_not_expect_purchase_provider_row(
    data, statement, tmp_path
):
    result = report(data, statement, tmp_path)
    assert result["status"] == "COMPLETE" and result["usable"] and result["reconciled"]
    assert result["issues"] == [] and result["matched_provider_keys"] == [PROVIDER_KEY]
    assert result["groups"] == [
        {
            "group_id": hashlib.sha256(
                encoded([NAMESPACE, BATCH, "PLN", M1, "quoted_fee_v1"])
            ).hexdigest(),
            "group_key": [NAMESPACE, BATCH, "PLN", M1, "quoted_fee_v1"],
            "row_ids": [ROW],
            "provider_keys": [PROVIDER_KEY],
            "principal_minor": "60",
            "fee_minor": "2",
            "gross_minor": "62",
            "matched": True,
        }
    ]
    assert result["internal_verification"]["healthy"]


def test_missing_internal_paid_provider_held_locally_is_not_excused_as_timing(
    snapshot, statement, tmp_path
):
    result = report(for_export(v6(snapshot)), statement, tmp_path)
    assert "MISSING_INTERNAL" in categories(result)
    assert "TIMING_ONLY" not in categories(result)
    assert not result["reconciled"] and result["unresolved_payout_ids"] == [PAYOUT]


def test_unexpected_internal_journal_of_rejected_intent(data, statement, tmp_path):
    data["operations"][-1].update(admission="REJECTED", http_status=422)
    result = report(data, statement, tmp_path)
    assert "UNEXPECTED_INTERNAL" in categories(result)
    assert "UNEXPECTED_PROVIDER" in categories(result)


def test_duplicate_effect_retains_both_physical_rows(data, statement, tmp_path):
    statement["effects"].append(copy.deepcopy(statement["effects"][0]))
    result = report(data, statement, tmp_path)
    assert "DUPLICATE_EFFECT" in categories(result) and result["matched_provider_keys"] == []
    assert result["usable"] and not result["reconciled"]


def test_equal_total_for_wrong_merchant_is_wrong_owner_before_amount(data, statement, tmp_path):
    statement["effects"][0]["request"]["merchant_id"] = M2
    statement["rows"][0]["group"]["merchant_id"] = M2
    rehash(statement)
    result = report(data, statement, tmp_path)
    assert "WRONG_OWNER" in categories(result) and "WRONG_AMOUNT" not in categories(result)
    assert result["matched_provider_keys"] == [] and not result["groups"][0]["matched"]


def test_wrong_principal_is_not_hidden_by_same_request_identity(data, statement, tmp_path):
    statement["effects"][0]["request"].update(principal_minor="61", gross_minor="63")
    statement["rows"][0].update(principal_minor="61", gross_minor="63")
    rehash(statement)
    result = report(data, statement, tmp_path)
    assert "WRONG_AMOUNT" in categories(result) and "CORRUPT_INPUT" not in categories(result)


def test_valid_different_currency_gets_its_own_category(data, statement, tmp_path):
    statement["effects"][0]["request"]["currency"] = "EUR"
    statement["rows"][0]["group"]["currency"] = "EUR"
    rehash(statement)
    result = report(data, statement, tmp_path)
    assert "CURRENCY_MISMATCH" in categories(result) and result["usable"]


def test_missing_provider_is_a_fixed_inventory_difference_not_claim_of_lost_cash(
    data, statement, tmp_path
):
    statement["effects"], statement["rows"] = [], []
    result = report(data, statement, tmp_path)
    assert categories(result) == {"MISSING_PROVIDER"}
    assert result["usable"] and not result["reconciled"]


def test_provider_effect_without_local_accepted_instruction_is_unexpected(
    data, statement, tmp_path
):
    data["operations"] = [row for row in data["operations"] if row["operation_id"] != PAYOUT]
    result = report(data, statement, tmp_path)
    assert "UNEXPECTED_PROVIDER" in categories(result)


def test_timing_only_requires_exact_economic_match_and_explicit_two_windows(
    data, statement, tmp_path
):
    statement["effects"][0]["assignment"]["effective_at"] = "2026-10-30T10:02:00Z"
    statement["rows"] = []
    result = report(data, statement, tmp_path)
    assert categories(result) == {"TIMING_ONLY"}
    assert result["matched_provider_keys"] == [PROVIDER_KEY] and not result["reconciled"]
    difference = result["issues"][0]
    assert difference["scope"] == "SETTLEMENT_WINDOW" and len(difference["refs"]) == 2


def test_wrong_fee_is_separate_from_principal(data, statement, tmp_path):
    statement["effects"][0]["request"].update(fee_minor="3", gross_minor="63")
    statement["rows"][0].update(fee_minor="3", gross_minor="63")
    rehash(statement)
    result = report(data, statement, tmp_path)
    assert "FEE_MISMATCH" in categories(result)


@pytest.mark.parametrize("fault", ["group", "members", "duplicate-allocation"])
def test_ambiguous_group_never_uses_subset_sum_to_guess(data, statement, tmp_path, fault):
    if fault == "group":
        statement["rows"][0]["group"] = None
    elif fault == "members":
        statement["rows"][0]["provider_keys"] = None
    else:
        statement["rows"][0]["provider_keys"].append(PROVIDER_KEY)
    result = report(data, statement, tmp_path)
    assert "AMBIGUOUS_GROUP" in categories(result) and not result["reconciled"]


def test_signed_summary_contradicting_members_is_not_trusted(data, statement, tmp_path):
    statement["rows"][0].update(principal_minor="61", gross_minor="63")
    result = report(data, statement, tmp_path)
    assert "INPUT_CONTRADICTION" in categories(result)
    assert not result["groups"][0]["matched"]
    assert result["matched_provider_keys"] == []


@pytest.mark.parametrize(
    "fault", ["signature", "future-schema", "duplicate-json", "nan", "invalid-json"]
)
def test_corrupt_input_is_an_unusable_report_not_a_partial_success(
    data, statement, tmp_path, fault
):
    if fault == "future-schema":
        statement["schema_version"] = 2
        raw = signed(statement)
    elif fault == "signature":
        value = json.loads(signed(statement))
        value["signature"] = "0" * 64
        raw = encoded(value)
    elif fault == "duplicate-json":
        raw = b'{"key_id":"x","key_id":"y"}'
    elif fault == "nan":
        raw = b'{"key_id":NaN}'
    else:
        raw = b"not json"
    result = report(data, statement, tmp_path, raw=raw)
    assert result["status"] == "CORRUPT_INPUT" and not result["usable"]
    assert categories(result) == {"CORRUPT_INPUT"} and result["groups"] == []


@pytest.mark.parametrize(
    "day,start,end,hours",
    [
        ("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", 23),
        ("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z", 25),
    ],
)
def test_business_day_uses_calendar_boundaries_not_86400_seconds(day, start, end, hours):
    assert day_bounds(day) == (start, end)
    assert (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds() == hours * 3600


def test_partitions_merge_identically_in_any_completion_order_and_reject_missing_coverage(
    data, statement, tmp_path
):
    path = publish_snapshot(data, tmp_path, code_revision=REVISION)
    bundle = load_export_bundle(path)
    prepared = prepare_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=4
    )
    parts = [
        match_partition(prepared, part["partition_id"])
        for part in prepared.partition_plan["partitions"]
    ]
    first = merge_partition_results(prepared, parts)
    assert first == merge_partition_results(prepared, parts[::-1])
    single = reconcile_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=1
    )
    assert semantic_digest(single) == semantic_digest(first)
    with pytest.raises(CorruptInput, match="INCOMPLETE_PARTITION_RESULTS"):
        merge_partition_results(prepared, parts[:-1])
    changed = copy.deepcopy(parts)
    next(part for part in changed if part["coverage"])["coverage"].pop()
    with pytest.raises(CorruptInput, match="PARTITION_COVERAGE_MISMATCH"):
        merge_partition_results(prepared, changed)


def test_loaded_export_pins_original_bytes_and_decoded_views_cannot_change_them(data, tmp_path):
    path = publish_snapshot(data, tmp_path, code_revision=REVISION)
    bundle = load_export_bundle(path)
    assert bundle.data_bytes == (path / "data.json").read_bytes()
    assert bundle.manifest_bytes == (path / "manifest.json").read_bytes()
    assert bundle.snapshot == load_export(path)
    changed = bundle.snapshot
    changed["balances"][0]["booked"] = "999"
    assert bundle.snapshot["balances"][0]["booked"] == "38"
    (path / "data.json").write_bytes(b"replaced after pin")
    assert bundle.snapshot["balances"][0]["booked"] == "38"


def test_statement_trust_does_not_print_or_retain_mutable_key_mapping(statement):
    keys = {KEY_ID: SECRET}
    trust = StatementTrust(NAMESPACE, keys)
    keys[KEY_ID] = b"different signing key over 32 bytes"
    assert parse_statement(signed(statement), trust=trust) == statement
    assert SECRET.decode() not in repr(trust)


@pytest.mark.parametrize(
    "fault",
    [
        "unknown-field",
        "revision",
        "counts",
        "dirty-bool",
        "size",
        "duplicate-json",
        "duplicate-operation",
        "duplicate-account",
        "duplicate-payout",
    ],
)
def test_raw_export_reconstruction_enforces_the_complete_filesystem_contract(data, tmp_path, fault):
    path = publish_snapshot(data, tmp_path, code_revision=REVISION)
    bundle = load_export_bundle(path)
    manifest, value = bundle.manifest, bundle.snapshot
    if fault == "unknown-field":
        manifest["unexpected"] = True
    elif fault == "revision":
        manifest["code_revision"] = "unverified"
    elif fault == "counts":
        manifest["row_counts"]["operations"] += 1
    elif fault == "dirty-bool":
        manifest["dirty"] = 1
    elif fault == "size":
        manifest["data_bytes"] += 1
    elif fault.startswith("duplicate-") and fault != "duplicate-json":
        table = {
            "duplicate-operation": "operations",
            "duplicate-account": "accounts",
            "duplicate-payout": "payouts",
        }[fault]
        value[table].append(copy.deepcopy(value[table][0]))
        manifest["row_counts"][table] += 1
        changed = encoded(value)
        manifest.update(data_bytes=len(changed), data_sha256=hashlib.sha256(changed).hexdigest())
    raw = bundle.data_bytes if value == bundle.snapshot else encoded(value)
    manifest_raw = encoded(manifest)
    if fault == "duplicate-json":
        manifest_raw = b'{"dirty":true,' + manifest_raw[1:]
    for constructor in (LoadedExport, validate_export_bytes):
        with pytest.raises(ExportError):
            constructor(manifest_raw, raw)


def test_raw_pinned_export_rejects_mutable_or_oversized_bytes(data, tmp_path):
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    with pytest.raises(ExportError):
        LoadedExport(bytearray(bundle.manifest_bytes), bundle.data_bytes)
    with pytest.raises(ExportError):
        LoadedExport(b" " * 65537, bundle.data_bytes)


@pytest.mark.parametrize("fault", ["row-owner", "row-fee", "statement-fee", "row-members"])
def test_group_or_source_failure_prevents_advertising_members_as_exact(
    data, statement, tmp_path, fault
):
    if fault == "row-owner":
        statement["rows"][0]["group"]["merchant_id"] = M2
    elif fault == "row-fee":
        statement["rows"][0].update(fee_minor="3", gross_minor="63")
    elif fault == "statement-fee":
        statement["fee_rule_version"] = "unknown_fee_v2"
    else:
        statement["rows"][0]["provider_keys"] = None
    result = report(data, statement, tmp_path)
    assert result["usable"] and not result["reconciled"]
    assert result["matched_provider_keys"] == []


def test_returned_partition_coverage_does_not_alias_prepared_plan(data, statement, tmp_path):
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    prepared = prepare_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=1
    )
    result = match_partition(prepared, 0)
    result["coverage"].pop()
    assert len(result["coverage"]) + 1 == len(prepared.partition_plan["partitions"][0]["coverage"])
    with pytest.raises(CorruptInput, match="PARTITION_COVERAGE_MISMATCH"):
        merge_partition_results(prepared, [result])


def second_provider_effect(statement):
    extra = copy.deepcopy(statement["effects"][0])
    identifier = "40000000-0000-4000-8000-000000000004"
    row_id = "a0000000-0000-4000-8000-000000000003"
    extra["provider_key"] = f"v1.{NAMESPACE}.{W}.{identifier}"
    extra["request"]["operation_id"] = identifier
    extra["execution_sequence"] = 2
    extra["assignment"].update(sequence=2, row_id=row_id)
    row = copy.deepcopy(statement["rows"][0])
    row.update(row_id=row_id, provider_keys=[extra["provider_key"]])
    statement["effects"].append(extra)
    statement["rows"].append(row)
    statement.update(effect_watermark=2, assignment_watermark=2)
    rehash(statement)
    return extra


def test_split_rows_aggregate_complete_group_without_guessing_missing_local_member(
    data, statement, tmp_path
):
    extra = second_provider_effect(statement)
    result = report(data, statement, tmp_path)
    assert categories(result) == {"UNEXPECTED_PROVIDER"}
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert len(group["row_ids"]) == 2 and group["provider_keys"] == sorted(
        [PROVIDER_KEY, extra["provider_key"]]
    )
    assert (group["principal_minor"], group["fee_minor"], group["gross_minor"]) == (
        "120",
        "4",
        "124",
    )
    assert not group["matched"]


@pytest.mark.parametrize("fault", ["execution-sequence", "assignment-sequence", "batch-time"])
def test_cross_member_source_contradictions_block_all_affected_exact_matches(
    data, statement, tmp_path, fault
):
    extra = second_provider_effect(statement)
    if fault == "execution-sequence":
        extra["execution_sequence"] = 1
    elif fault == "assignment-sequence":
        extra["assignment"]["sequence"] = 1
    else:
        extra["assignment"]["effective_at"] = "2026-10-29T11:02:00Z"
    result = report(data, statement, tmp_path)
    assert "INPUT_CONTRADICTION" in categories(result)
    assert result["matched_provider_keys"] == [] and not result["groups"][0]["matched"]


@pytest.mark.parametrize(
    "effective,inside",
    [
        ("2026-10-28T23:00:00Z", True),
        ("2026-10-29T23:00:00Z", False),
    ],
)
def test_settlement_window_start_inclusive_end_exclusive(
    data, statement, tmp_path, effective, inside
):
    statement["effects"][0]["assignment"]["effective_at"] = effective
    if not inside:
        statement["rows"] = []
    result = report(data, statement, tmp_path)
    assert categories(result) == (set() if inside else {"TIMING_ONLY"})


def test_other_provider_held_payout_is_separate_from_selected_namespace(
    data, snapshot, statement, tmp_path
):
    extra = for_export(v6(snapshot))
    payout_id = "40000000-0000-4000-8000-000000000004"
    namespace = "70000000-0000-4000-8000-000000000009"

    def adapt(value, field=None):
        if isinstance(value, dict):
            result = {key: adapt(item, key) for key, item in value.items()}
            if "payload_hex" in result:
                raw = bytes.fromhex(value["payload_hex"])
                prefix, body = raw.split(b"\n", 1)
                request = adapt(json.loads(body))
                raw = prefix + b"\n" + json.dumps(request, separators=(",", ":")).encode()
                result.update(payload_hex=raw.hex(), payload_sha256=hashlib.sha256(raw).hexdigest())
            if "provider_request_hex" in result:
                raw = encoded(adapt(json.loads(bytes.fromhex(value["provider_request_hex"]))))
                result.update(
                    provider_request_hex=raw.hex(),
                    provider_request_sha256=hashlib.sha256(raw).hexdigest(),
                )
            return result
        if value == PAYOUT:
            return payout_id
        if value == NAMESPACE:
            return namespace
        if value == PROVIDER_KEY:
            return f"v1.{namespace}.{W}.{payout_id}"
        if field == "principal_minor":
            return "10"
        if field == "fee_minor":
            return "1"
        if field == "gross_minor":
            return "11"
        return value

    for table, identity in (
        ("operations", "operation_id"),
        ("requests", "operation_id"),
        ("payouts", "operation_id"),
        ("reservations", "payout_id"),
        ("reservation_events", "payout_id"),
        ("dispatch_intents", "payout_id"),
    ):
        rows = [row for row in extra[table] if row[identity] == PAYOUT]
        for row in rows:
            updated = adapt(row)
            if table == "requests":
                updated["key"] = "other-provider-pending"
            data[table].append(updated)
    data["requests"].sort(key=lambda row: (row["client_id"], row["route_family"], row["key"]))
    data["balances"][2]["reserved"] = "11"
    data["balances"][2]["version"] += 1
    result = report(data, statement, tmp_path)
    assert result["internal_verification"]["healthy"] and result["reconciled"]
    assert result["matched_provider_keys"] == [PROVIDER_KEY]
    assert result["unresolved_payout_ids"] == []
    assert result["workspace_unresolved_payout_ids"] == [payout_id]


def test_paid_and_posted_but_unassigned_is_explicitly_incomplete_settlement(
    data, statement, tmp_path
):
    statement["effects"][0]["assignment"] = None
    statement["rows"] = []
    statement["assignment_watermark"] = 0
    result = report(data, statement, tmp_path)
    assert result["usable"] and result["internal_verification"]["healthy"]
    assert result["matched_provider_keys"] == [PROVIDER_KEY]
    assert result["issues"] == [] and result["unresolved_payout_ids"] == []
    assert result["unsettled_provider_keys"] == [PROVIDER_KEY]
    assert not result["settlement_complete"] and not result["reconciled"]


def test_duplicate_local_journals_are_effect_duplicates_not_one_dictionary_winner(
    data, statement, tmp_path
):
    duplicate = copy.deepcopy(next(row for row in data["journals"] if row["journal_id"] == JOURNAL))
    duplicate["journal_id"] = "50000000-0000-4000-8000-000000000004"
    data["journals"].append(duplicate)
    data["entries"].extend(
        {**row, "journal_id": duplicate["journal_id"]}
        for row in list(data["entries"])
        if row["journal_id"] == JOURNAL
    )
    result = report(data, statement, tmp_path)
    assert "DUPLICATE_EFFECT" in categories(result) and result["matched_provider_keys"] == []


def test_balanced_local_wrong_merchant_is_not_hidden_by_correct_provider_total(
    data, statement, tmp_path
):
    for row in data["entries"]:
        if row["journal_id"] == JOURNAL and row["side"] == "D":
            row["account_id"] = M2
    result = report(data, statement, tmp_path)
    assert "WRONG_OWNER" in categories(result) and result["matched_provider_keys"] == []


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("gross", "GROUP_SUMMARY_MISMATCH"),
        ("members", "GROUP_SUMMARY_MISMATCH"),
        ("global", "INVALID_GLOBAL_COVERAGE"),
        ("fabricated-key", "MATCH_COVERAGE_MISMATCH"),
    ],
)
def test_worker_result_with_recomputed_digest_still_needs_source_bound_summary(
    data, statement, tmp_path, fault, expected
):
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    prepared = prepare_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=1
    )
    part = match_partition(prepared, 0)
    if fault == "gross":
        part["result"]["groups"][0]["gross_minor"] = "999"
    elif fault == "members":
        part["result"]["groups"][0]["provider_keys"] = []
    elif fault == "global":
        part["result"]["global_checks"] = False
    else:
        part["result"]["matched_provider_keys"] = ["v1." + ".".join([NAMESPACE, W, W])]
    assert len(semantic_digest(part["result"])) == 64
    with pytest.raises(CorruptInput, match=expected):
        merge_partition_results(prepared, [part])


@pytest.mark.parametrize("fault", ["source-gross", "source-owner", "source-fee"])
def test_worker_cannot_hide_source_summary_contradiction_by_removing_its_diagnostic(
    data, statement, tmp_path, fault
):
    if fault == "source-gross":
        statement["rows"][0]["gross_minor"] = "999"
    elif fault == "source-owner":
        statement["rows"][0]["group"]["merchant_id"] = M2
    else:
        statement["rows"][0].update(fee_minor="3", gross_minor="63")
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    prepared = prepare_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=1
    )
    part = match_partition(prepared, 0)
    assert part["result"]["issues"]
    part["result"]["issues"] = []
    part["result"]["groups"][0]["matched"] = True
    part["result"]["matched_provider_keys"] = [PROVIDER_KEY]
    assert len(semantic_digest(part["result"])) == 64
    with pytest.raises(CorruptInput, match="UNSUPPORTED_GROUP_MATCH"):
        merge_partition_results(prepared, [part])


def test_worker_omitting_all_match_claims_cannot_make_green_report(data, statement, tmp_path):
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision=REVISION))
    prepared = prepare_inputs(
        bundle, signed(statement), trust=TRUST, code_revision=REVISION, partitions=1
    )
    part = match_partition(prepared, 0)
    part["result"]["issues"] = []
    part["result"]["groups"][0]["matched"] = False
    part["result"]["matched_provider_keys"] = []
    result = merge_partition_results(prepared, [part])
    assert not result["reconciled"]
    assert result["unmatched_provider_keys"] == [PROVIDER_KEY]
