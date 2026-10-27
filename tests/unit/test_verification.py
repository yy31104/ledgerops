"""Hand-authored exported rows; never call production posting/canonical helpers."""

import copy
import hashlib
import json

import pytest

from ledgerops.verification import verify_snapshot

W = "10000000-0000-4000-8000-000000000001"
CLEARING = "30000000-0000-4000-8000-000000000001"
CUSTOMER = "30000000-0000-4000-8000-000000000002"
M1 = "30000000-0000-4000-8000-000000000003"
M2 = "30000000-0000-4000-8000-000000000004"
OPENING = "40000000-0000-4000-8000-000000000001"
PURCHASE = "40000000-0000-4000-8000-000000000002"
J_OPENING = "50000000-0000-4000-8000-000000000001"
J_PURCHASE = "50000000-0000-4000-8000-000000000002"
TIME = "2026-10-27T10:00:00+00:00"
PURCHASE_BYTES = (
    b'ledgerops.purchase:v1\n{"workspace_id":"10000000-0000-4000-8000-000000000001",'
    b'"operation_id":"40000000-0000-4000-8000-000000000002",'
    b'"customer_account_id":"30000000-0000-4000-8000-000000000002",'
    b'"merchant_account_id":"30000000-0000-4000-8000-000000000003",'
    b'"amount_minor":"80","currency":"PLN"}'
)


def bytes_fields(payload):
    return {"payload_hex": payload.hex(), "payload_sha256": hashlib.sha256(payload).hexdigest()}


@pytest.fixture
def snapshot():
    """Literal opening100, purchase80, all four balances, one retained request."""
    accounts = []
    for index, identifier in enumerate((CLEARING, CUSTOMER, M1, M2), 1):
        accounts.append(
            {
                "workspace_id": W,
                "id": identifier,
                "owner_id": f"20000000-0000-4000-8000-{index:012}",
                "class": "SYNTHETIC_CLEARING"
                if index == 1
                else ("CUSTOMER_LIABILITY" if index == 2 else "MERCHANT_LIABILITY"),
                "normal_side": "D" if index == 1 else "C",
                "currency": "PLN",
                "enabled": True,
            }
        )
    body = {
        "operation_id": PURCHASE,
        "admission": "accepted",
        "journal_id": J_PURCHASE,
        "amount_minor": "80",
        "currency": "PLN",
        "effect": "purchase_posted",
        "contract_version": 1,
    }
    return {
        "format_version": 1,
        "workspace": {"id": W, "currency": "PLN", "mode": "NORMAL", "schema_epoch": 1},
        "snapshot_witness": "100:100:",
        "schema_versions": ["001_foundation.sql", "002_export_reader.sql"],
        "server_version": "18.6",
        "accounts": accounts,
        "balances": [
            {
                "workspace_id": W,
                "account_id": identifier,
                "normal_side": side,
                "debits": debit,
                "credits": credit,
                "booked": booked,
                "reserved": "0",
                "version": version,
            }
            for identifier, side, debit, credit, booked, version in [
                (CLEARING, "D", "100", "0", "100", 1),
                (CUSTOMER, "C", "80", "100", "20", 2),
                (M1, "C", "0", "80", "80", 1),
                (M2, "C", "0", "0", "0", 0),
            ]
        ],
        "operations": [
            {
                "workspace_id": W,
                "operation_id": OPENING,
                "kind": "OPENING",
                **bytes_fields(b"explicit fixture operation"),
                "admission": "ACCEPTED",
                "http_status": 201,
                "response_body": {"operation_id": OPENING, "fixture": True},
                "created_at": TIME,
            },
            {
                "workspace_id": W,
                "operation_id": PURCHASE,
                "kind": "PURCHASE",
                **bytes_fields(PURCHASE_BYTES),
                "admission": "ACCEPTED",
                "http_status": 201,
                "response_body": body,
                "created_at": TIME,
            },
        ],
        "requests": [
            {
                "workspace_id": W,
                "client_id": "60000000-0000-4000-8000-000000000001",
                "route_family": "purchase:v1",
                "key": "first-purchase",
                "operation_id": PURCHASE,
                **bytes_fields(PURCHASE_BYTES),
                "http_status": 201,
                "response_body": copy.deepcopy(body),
            }
        ],
        "journals": [
            {
                "workspace_id": W,
                "journal_id": J_OPENING,
                "operation_id": OPENING,
                "kind": "OPENING",
                "line_count": 2,
                "total_minor": "100",
                "recorded_at": TIME,
            },
            {
                "workspace_id": W,
                "journal_id": J_PURCHASE,
                "operation_id": PURCHASE,
                "kind": "PURCHASE",
                "line_count": 2,
                "total_minor": "80",
                "recorded_at": TIME,
            },
        ],
        "entries": [
            {
                "workspace_id": W,
                "journal_id": journal,
                "line_no": number,
                "account_id": account,
                "side": side,
                "amount_minor": amount,
            }
            for journal, number, account, side, amount in [
                (J_OPENING, 1, CLEARING, "D", "100"),
                (J_OPENING, 2, CUSTOMER, "C", "100"),
                (J_PURCHASE, 1, CUSTOMER, "D", "80"),
                (J_PURCHASE, 2, M1, "C", "80"),
            ]
        ],
    }


def codes(report):
    return {issue.code for issue in report.issues}


def test_literal_snapshot_passes_without_external_manifest_or_mutation(snapshot):
    before = copy.deepcopy(snapshot)
    report = verify_snapshot(snapshot)
    assert report.healthy and report.rebuild_eligible
    assert report.issues == ()
    assert report.intent_scope == ("PURCHASE",)
    assert report.unverified_intent_kinds == ("OPENING",)
    assert snapshot == before
    serialized = json.loads(json.dumps(report.as_dict()))
    assert serialized["healthy"] is True
    assert serialized["unverified_intent_kinds"] == ["OPENING"]


def test_consistent_wrong_merchant_passes_fold_but_fails_saved_request(snapshot):
    snapshot["entries"][-1]["account_id"] = M2
    snapshot["balances"][2].update(credits="0", booked="0", version=0)
    snapshot["balances"][3].update(credits="80", booked="80", version=1)
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.projection_ok
    assert not report.request_intent_ok and not report.rebuild_eligible
    assert "WRONG_OWNER" in codes(report)


def test_missed_customer_projection_is_repairable_not_false_intent_failure(snapshot):
    snapshot["balances"][1].update(debits="0", booked="100", version=1)
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.request_intent_ok
    assert not report.projection_ok and not report.healthy
    assert report.rebuild_eligible
    assert {i.field for i in report.issues if i.code == "PROJECTION_DRIFT"} == {"debits", "booked"}


def test_missing_idle_projection_is_detected_and_remains_repairable(snapshot):
    snapshot["balances"].pop()
    report = verify_snapshot(snapshot)
    assert not report.projection_ok and report.rebuild_eligible
    assert [(i.code, i.object_id) for i in report.issues] == [("MISSING_PROJECTION", M2)]


def test_consistent_wrong_amount_fails_original_request(snapshot):
    for line in snapshot["entries"][-2:]:
        line["amount_minor"] = "81"
    snapshot["journals"][-1]["total_minor"] = "81"
    snapshot["balances"][1].update(debits="81", booked="19")
    snapshot["balances"][2].update(credits="81", booked="81")
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.projection_ok
    assert not report.request_intent_ok and "WRONG_AMOUNT" in codes(report)


@pytest.mark.parametrize("fault", ["digest", "prefix", "noncanonical", "duplicate-json", "scope"])
def test_bad_saved_request_cannot_be_accepted_as_intent(snapshot, fault):
    raw = PURCHASE_BYTES
    if fault == "digest":
        snapshot["operations"][1]["payload_sha256"] = "0" * 64
    else:
        if fault == "prefix":
            raw = raw.replace(b"purchase:v1", b"purchase:v2")
        elif fault == "noncanonical":
            raw = raw.replace(b"\n{", b"\n {")
        elif fault == "duplicate-json":
            raw = raw[:-1] + b',"currency":"PLN"}'
        else:
            raw = raw.replace(W.encode(), b"10000000-0000-4000-8000-000000000099")
        snapshot["operations"][1].update(bytes_fields(raw))
    report = verify_snapshot(snapshot)
    assert not report.request_intent_ok and not report.rebuild_eligible
    assert {"INVALID_OPERATION", "INVALID_STORED_REQUEST"}.intersection(codes(report))


@pytest.mark.parametrize(
    "fault", ["bool-version", "request-status", "request-body", "request-payload"]
)
def test_bad_result_bindings_and_json_types_are_reported(snapshot, fault):
    if fault == "bool-version":
        for row in [snapshot["operations"][1], snapshot["requests"][0]]:
            row["response_body"]["contract_version"] = True
    elif fault == "request-status":
        snapshot["requests"][0]["http_status"] = 422
    elif fault == "request-body":
        snapshot["requests"][0]["response_body"]["contract_version"] = True
    else:
        snapshot["requests"][0].update(bytes_fields(PURCHASE_BYTES.replace(b'"80"', b'"81"')))
    report = verify_snapshot(snapshot)
    assert not report.request_intent_ok and not report.rebuild_eligible
    assert {"INVALID_ACCEPTANCE", "REQUEST_OPERATION_MISMATCH"}.intersection(codes(report))


@pytest.mark.parametrize(
    "table", ["accounts", "balances", "operations", "requests", "journals", "entries"]
)
def test_cross_scope_rows_are_rejected(snapshot, table):
    snapshot[table][0]["workspace_id"] = "10000000-0000-4000-8000-000000000099"
    report = verify_snapshot(snapshot)
    assert not report.structure_ok and not report.rebuild_eligible
    assert "CROSS_SCOPE" in codes(report)


@pytest.mark.parametrize(
    "fault", ["empty-journal", "missing-journal", "duplicate-line", "missing-account"]
)
def test_missing_or_duplicate_journal_structure_is_detected(snapshot, fault):
    if fault == "empty-journal":
        snapshot["entries"] = snapshot["entries"][:2]
    elif fault == "missing-journal":
        snapshot["journals"].pop()
    elif fault == "duplicate-line":
        snapshot["entries"].append(copy.deepcopy(snapshot["entries"][-1]))
    else:
        snapshot["accounts"].pop(2)
    report = verify_snapshot(snapshot)
    assert not report.structure_ok and not report.rebuild_eligible


def test_reservations_are_not_guessed_from_posted_entries(snapshot):
    snapshot["balances"][2]["reserved"] = "40"
    report = verify_snapshot(snapshot)
    assert report.healthy
    assert not report.reservation_supported and not report.rebuild_eligible
    assert "UNSUPPORTED_RESERVATION" in codes(report)


def test_rejected_request_does_not_reconsider_current_funds(snapshot):
    operation = snapshot["operations"][1]
    rejection = {
        "error": {
            "code": "INSUFFICIENT_AVAILABLE",
            "message": "Insufficient available balance",
            "retryable": False,
            "operation_id": PURCHASE,
            "request_id": "70000000-0000-4000-8000-000000000001",
        }
    }
    operation.update(admission="REJECTED", http_status=422, response_body=rejection)
    snapshot["requests"][0].update(http_status=422, response_body=copy.deepcopy(rejection))
    snapshot["journals"].pop()
    snapshot["entries"] = snapshot["entries"][:2]
    snapshot["balances"][1].update(debits="0", booked="100", version=1)
    snapshot["balances"][2].update(credits="0", booked="0", version=0)
    report = verify_snapshot(snapshot)
    assert report.healthy and report.rebuild_eligible


@pytest.mark.parametrize("bad", [True, 2, "1", None])
def test_version_is_strict_not_python_bool_equal_to_one(snapshot, bad):
    snapshot["format_version"] = bad
    assert not verify_snapshot(snapshot).structure_ok


@pytest.mark.parametrize("fault", ["missing-table", "bad-row", "unhashable-key", "bad-money"])
def test_malformed_inputs_produce_issues_not_crashes(snapshot, fault):
    if fault == "missing-table":
        del snapshot["entries"]
    elif fault == "bad-row":
        snapshot["entries"][0] = None
    elif fault == "unhashable-key":
        snapshot["requests"][0]["key"] = []
    else:
        snapshot["balances"][1]["booked"] = 20.0
    report = verify_snapshot(snapshot)
    assert not report.structure_ok and not report.rebuild_eligible


def test_future_reversal_schema_is_not_silently_claimed_supported(snapshot):
    snapshot["operations"][1]["kind"] = "REVERSAL"
    report = verify_snapshot(snapshot)
    assert not report.structure_ok and not report.request_intent_ok


def test_captured_request_is_authority_not_proof_of_external_user_intent(snapshot):
    # A privileged actor can corrupt the saved instruction as well as its effect.
    # Runtime consistency cannot detect that; the independent test manifest must.
    changed = PURCHASE_BYTES.replace(M1.encode(), M2.encode())
    snapshot["operations"][1].update(bytes_fields(changed))
    snapshot["requests"][0].update(bytes_fields(changed))
    snapshot["entries"][-1]["account_id"] = M2
    snapshot["balances"][2].update(credits="0", booked="0", version=0)
    snapshot["balances"][3].update(credits="80", booked="80", version=1)
    report = verify_snapshot(snapshot)
    assert report.healthy and report.rebuild_eligible
    # This literal original is outside the supplied stored rows and differs.
    assert changed != PURCHASE_BYTES
    assert json.loads(changed.split(b"\n", 1)[1])["merchant_account_id"] != M1


def test_missing_request_and_orphan_projection_are_not_healthy(snapshot):
    snapshot["requests"] = []
    orphan = copy.deepcopy(snapshot["balances"][3])
    orphan["account_id"] = "30000000-0000-4000-8000-000000000099"
    snapshot["balances"].append(orphan)
    report = verify_snapshot(snapshot)
    assert not report.structure_ok and not report.request_intent_ok
    assert not report.rebuild_eligible
    assert {"MISSING_REQUEST", "EXTRA_PROJECTION"} <= codes(report)


def test_deeply_nested_retained_payload_reports_failure_instead_of_crashing(snapshot):
    malformed = b"ledgerops.purchase:v1\n" + b"[" * 20000 + b"0" + b"]" * 20000
    for row in (snapshot["operations"][1], snapshot["requests"][0]):
        row.update(bytes_fields(malformed))
    report = verify_snapshot(snapshot)
    assert not report.request_intent_ok and not report.rebuild_eligible
    assert any(issue.code == "INVALID_STORED_REQUEST" for issue in report.issues)


@pytest.fixture
def maintenance_snapshot(snapshot):
    freeze_id = "80000000-0000-4000-8000-000000000001"
    repair_id = "80000000-0000-4000-8000-000000000002"
    actor = "20000000-0000-4000-8000-000000000099"
    snapshot["format_version"] = 2
    snapshot["schema_versions"].append("003_projection_maintenance.sql")
    snapshot["workspace"].update(mode="FROZEN", projection_epoch=2, current_freeze_id=freeze_id)
    snapshot["maintenance_freezes"] = [
        dict(
            workspace_id=W,
            freeze_id=freeze_id,
            actor_id=actor,
            reason="inspect",
            frozen_at=TIME,
            resumed_at=None,
            resumed_by=None,
            resume_reason=None,
        )
    ]
    snapshot["repairs"] = [
        dict(
            workspace_id=W,
            repair_id=repair_id,
            freeze_id=freeze_id,
            actor_id=actor,
            reason="recompute",
            preview_export_id="90000000-0000-4000-8000-000000000001",
            source_witness="100:100:",
            old_epoch=1,
            new_epoch=2,
            account_count=len(snapshot["accounts"]),
            applied_at=TIME,
        )
    ]
    snapshot["repair_rows"] = []
    for balance in snapshot["balances"]:
        audit = dict(
            workspace_id=W,
            repair_id=repair_id,
            account_id=balance["account_id"],
            normal_side=balance["normal_side"],
        )
        for field in ("debits", "credits", "reserved", "booked", "version"):
            audit["before_" + field] = balance[field]
            audit["after_" + field] = balance[field] + 1 if field == "version" else balance[field]
        balance["version"] += 1
        snapshot["repair_rows"].append(audit)
    return snapshot


def test_maintenance_audit_format_checks_without_changing_purchase_intent(maintenance_snapshot):
    report = verify_snapshot(maintenance_snapshot)
    assert report.healthy and report.rebuild_eligible
    assert report.unverified_intent_kinds == ("OPENING",)
    assert report.issues == ()


def test_missing_projection_repair_audit_starts_version_one(maintenance_snapshot):
    audit = maintenance_snapshot["repair_rows"][-1]
    for field in ("debits", "credits", "reserved", "booked", "version"):
        audit["before_" + field] = None
    audit["after_version"] = 1
    assert verify_snapshot(maintenance_snapshot).healthy


def test_closed_freeze_remains_valid_immutable_history(maintenance_snapshot):
    maintenance_snapshot["workspace"].update(mode="NORMAL", current_freeze_id=None)
    maintenance_snapshot["maintenance_freezes"][0].update(
        resumed_at=TIME,
        resumed_by="20000000-0000-4000-8000-000000000099",
        resume_reason="post-check passed",
    )
    assert verify_snapshot(maintenance_snapshot).healthy


@pytest.mark.parametrize(
    "fault",
    [
        "open-freeze",
        "partial-resume",
        "missing-freeze",
        "epoch-gap",
        "row-count",
        "wrong-account",
        "wrong-side",
        "before-null",
        "after-money",
        "after-version",
        "reserved",
        "missing-new-table",
        "cross-scope",
        "bad-witness",
    ],
)
def test_malformed_maintenance_history_blocks_repair(maintenance_snapshot, fault):
    freeze = maintenance_snapshot["maintenance_freezes"][0]
    repair = maintenance_snapshot["repairs"][0]
    row = maintenance_snapshot["repair_rows"][0]
    if fault == "open-freeze":
        maintenance_snapshot["workspace"]["current_freeze_id"] = None
    elif fault == "partial-resume":
        freeze["resumed_at"] = TIME
    elif fault == "missing-freeze":
        repair["freeze_id"] = "80000000-0000-4000-8000-000000000099"
    elif fault == "epoch-gap":
        maintenance_snapshot["workspace"]["projection_epoch"] = 4
    elif fault == "row-count":
        maintenance_snapshot["repair_rows"].pop()
    elif fault == "wrong-account":
        row["account_id"] = "30000000-0000-4000-8000-000000000099"
    elif fault == "wrong-side":
        row["normal_side"] = "C"
    elif fault == "before-null":
        row["before_version"] = None
    elif fault == "after-money":
        row["after_booked"] = "0"
    elif fault == "after-version":
        row["after_version"] = True
    elif fault == "reserved":
        row["before_reserved"] = "1"
    elif fault == "missing-new-table":
        del maintenance_snapshot["repairs"]
    elif fault == "cross-scope":
        row["workspace_id"] = "10000000-0000-4000-8000-000000000099"
    else:
        repair["source_witness"] = ""
    report = verify_snapshot(maintenance_snapshot)
    assert not report.structure_ok and not report.rebuild_eligible
    assert report.request_intent_ok
