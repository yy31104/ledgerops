"""Literal payout histories; no admission, posting or provider implementation helpers."""

import copy
import hashlib
import json

import pytest
from test_verification import M1, M2, W, bytes_fields, codes
from test_verification import maintenance_snapshot as maintenance_snapshot
from test_verification import snapshot as snapshot

from ledgerops.export import ExportError, load_export, publish_snapshot
from ledgerops.verification import verify_snapshot

PAYOUT = "40000000-0000-4000-8000-000000000003"
BENEFICIARY = "70000000-0000-4000-8000-000000000001"
NAMESPACE = "70000000-0000-4000-8000-000000000002"
ATTEMPT = "70000000-0000-4000-8000-000000000003"
STAMP = "2026-10-29T10:00:00.000000Z"
DEADLINE = "2026-10-29T10:01:00Z"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def add_payout(data):
    request = {
        "workspace_id": W,
        "operation_id": PAYOUT,
        "merchant_account_id": M1,
        "beneficiary_id": BENEFICIARY,
        "provider_namespace": NAMESPACE,
        "principal_minor": "60",
        "fee_minor": "2",
        "currency": "PLN",
        "contract_version": 1,
    }
    raw = b"ledgerops.payout:v1\n" + json.dumps(request, separators=(",", ":")).encode()
    transmitted = {
        "workspace_id": W,
        "operation_id": PAYOUT,
        "merchant_id": M1,
        "beneficiary_id": BENEFICIARY,
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": DEADLINE,
    }
    provider_raw = json.dumps(transmitted, sort_keys=True, separators=(",", ":")).encode()
    body = {
        "operation_id": PAYOUT,
        "payout_id": PAYOUT,
        "admission": "accepted_for_processing",
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "dispatch_deadline": DEADLINE,
        "contract_version": 1,
    }
    data.update(format_version=5)
    data["schema_versions"] = [
        "001_foundation.sql",
        "002_export_reader.sql",
        "003_projection_maintenance.sql",
        "004_resume_review.sql",
        "005_full_reversal.sql",
        "006_payout_admission.sql",
    ]
    data["workspace"].setdefault("projection_epoch", 1)
    data["workspace"].setdefault("current_freeze_id", None)
    for table in (
        "maintenance_freezes",
        "repairs",
        "repair_rows",
        "reversal_attempts",
        "reversal_effects",
    ):
        data.setdefault(table, [])
    for freeze in data["maintenance_freezes"]:
        freeze.setdefault("resume_report", None)
    for repair in data["repairs"]:
        repair.setdefault("fold_version", 1)
    data["operations"].append(
        dict(
            workspace_id=W,
            operation_id=PAYOUT,
            kind="PAYOUT",
            **bytes_fields(raw),
            admission="ACCEPTED",
            http_status=202,
            response_body=body,
            created_at=STAMP,
        )
    )
    data["requests"].append(
        dict(
            workspace_id=W,
            client_id="60000000-0000-4000-8000-000000000002",
            route_family="payout:v1",
            key="first-payout",
            operation_id=PAYOUT,
            **bytes_fields(raw),
            http_status=202,
            response_body=copy.deepcopy(body),
        )
    )
    data["payouts"] = [
        dict(
            workspace_id=W,
            operation_id=PAYOUT,
            merchant_account_id=M1,
            beneficiary_id=BENEFICIARY,
            provider_namespace=NAMESPACE,
            provider_key=f"v1.{NAMESPACE}.{W}.{PAYOUT}",
            principal_minor="60",
            fee_minor="2",
            gross_minor="62",
            currency="PLN",
            contract_version=1,
            dispatch_deadline="2026-10-29T10:01:00.000000Z",
            provider_request_hex=provider_raw.hex(),
            provider_request_sha256=hashlib.sha256(provider_raw).hexdigest(),
            knowledge="NOT_SENT",
            version=1,
        )
    ]
    data["reservations"] = [
        dict(
            workspace_id=W,
            payout_id=PAYOUT,
            account_id=M1,
            gross_minor="62",
            state="HELD",
            version=1,
        )
    ]
    data["reservation_events"] = [
        dict(
            workspace_id=W,
            payout_id=PAYOUT,
            event_no=1,
            state="HELD",
            observation_id=None,
            recorded_at=STAMP,
        )
    ]
    data["dispatch_intents"] = [
        dict(
            workspace_id=W,
            payout_id=PAYOUT,
            state="READY",
            next_action="PUT",
            next_due_at=STAMP,
            attempts=0,
            last_attempt_id=None,
        )
    ]
    data["transport_attempts"] = []
    data["balances"][2]["reserved"] = "62"
    data["balances"][2]["version"] += 1
    return data


@pytest.fixture
def payout_snapshot(snapshot):
    return add_payout(snapshot)


def test_literal_hold_has_independent_reservation_projection_and_limited_coverage(payout_snapshot):
    before = copy.deepcopy(payout_snapshot)
    report = verify_snapshot(payout_snapshot)
    assert report.healthy and report.rebuild_eligible and report.reservation_supported
    assert report.issues == ()
    assert report.intent_scope == ("PURCHASE", "REVERSAL", "PAYOUT")
    assert report.unverified_intent_kinds == ("OPENING", "PAYOUT_EXTERNAL_RESULT")
    assert payout_snapshot == before


@pytest.mark.parametrize("reserved", ["0", "61", "63", "80"])
def test_reservation_projection_drift_is_repairable(payout_snapshot, reserved):
    payout_snapshot["balances"][2]["reserved"] = reserved
    report = verify_snapshot(payout_snapshot)
    assert report.structure_ok and report.request_intent_ok and report.reservation_supported
    assert not report.projection_ok and report.rebuild_eligible
    assert [(i.object_id, i.field, i.expected, i.actual) for i in report.issues] == [
        (M1, "reserved", "62", reserved)
    ]


def test_missing_projection_with_valid_hold_is_repairable(payout_snapshot):
    payout_snapshot["balances"].pop(2)
    report = verify_snapshot(payout_snapshot)
    assert report.rebuild_eligible and not report.projection_ok
    assert "MISSING_PROJECTION" in codes(report)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-event",
        "duplicate-event",
        "second-held",
        "terminal",
        "observation",
        "missing-hold",
        "wrong-hold-account",
        "wrong-hold-amount",
        "bad-version",
        "missing-intent",
        "orphan-event",
        "cross-scope",
        "missing-table",
    ],
)
def test_illegal_hold_source_never_becomes_a_rebuild_candidate(payout_snapshot, fault):
    if fault == "missing-event":
        payout_snapshot["reservation_events"] = []
    elif fault == "duplicate-event":
        payout_snapshot["reservation_events"] *= 2
    elif fault == "second-held":
        row = copy.deepcopy(payout_snapshot["reservation_events"][0])
        row["event_no"] = 2
        payout_snapshot["reservation_events"].append(row)
    elif fault == "terminal":
        payout_snapshot["reservation_events"][0]["state"] = "RELEASED"
    elif fault == "observation":
        payout_snapshot["reservation_events"][0]["observation_id"] = ATTEMPT
    elif fault == "missing-hold":
        payout_snapshot["reservations"] = []
    elif fault == "wrong-hold-account":
        payout_snapshot["reservations"][0]["account_id"] = M2
    elif fault == "wrong-hold-amount":
        payout_snapshot["reservations"][0]["gross_minor"] = "61"
    elif fault == "bad-version":
        payout_snapshot["reservations"][0]["version"] = True
    elif fault == "missing-intent":
        payout_snapshot["dispatch_intents"] = []
    elif fault == "orphan-event":
        payout_snapshot["reservation_events"][0]["payout_id"] = ATTEMPT
    elif fault == "cross-scope":
        payout_snapshot["reservations"][0]["workspace_id"] = NAMESPACE
    else:
        del payout_snapshot["reservation_events"]
    report = verify_snapshot(payout_snapshot)
    assert not report.rebuild_eligible and not report.healthy


@pytest.mark.parametrize(
    "fault",
    [
        "beneficiary",
        "provider-key",
        "provider-digest",
        "provider-bytes",
        "deadline",
        "fee",
        "wrong-202",
        "bad-body",
        "canonical",
        "duplicate-json",
        "request-mismatch",
    ],
)
def test_payout_intent_corruption_blocks_rebuild(payout_snapshot, fault):
    row = payout_snapshot["payouts"][0]
    if fault == "beneficiary":
        row["beneficiary_id"] = ATTEMPT
    elif fault == "provider-key":
        row["provider_key"] = row["provider_key"].replace(PAYOUT, ATTEMPT)
    elif fault == "provider-digest":
        row["provider_request_sha256"] = "0" * 64
    elif fault == "provider-bytes":
        raw = bytes.fromhex(row["provider_request_hex"]).replace(b'"60"', b'"59"')
        row.update(
            provider_request_hex=raw.hex(), provider_request_sha256=hashlib.sha256(raw).hexdigest()
        )
    elif fault == "deadline":
        row["dispatch_deadline"] = "2026-10-29T10:02:00Z"
    elif fault == "fee":
        row["fee_minor"] = "3"
    elif fault == "wrong-202":
        payout_snapshot["operations"][-1]["http_status"] = 201
    elif fault == "bad-body":
        payout_snapshot["operations"][-1]["response_body"]["gross_minor"] = "60"
    elif fault in {"canonical", "duplicate-json"}:
        raw = bytes.fromhex(payout_snapshot["operations"][-1]["payload_hex"])
        raw += b" " if fault == "canonical" else b""
        if fault == "duplicate-json":
            raw = raw.replace(b'{"workspace_id":', b'{"currency":"PLN","workspace_id":')
        payout_snapshot["operations"][-1].update(bytes_fields(raw))
    else:
        payout_snapshot["requests"][-1]["payload_hex"] = "01"
    report = verify_snapshot(payout_snapshot)
    assert not report.request_intent_ok and not report.rebuild_eligible


def test_consistent_wrong_merchant_still_fails_original_request(payout_snapshot):
    # Fund the other merchant in the literal journal/projection; all balances fold.
    payout_snapshot["entries"][-1]["account_id"] = M2
    payout_snapshot["balances"][2].update(credits="0", booked="0", reserved="0")
    payout_snapshot["balances"][3].update(credits="80", booked="80", reserved="62")
    payout_snapshot["payouts"][0]["merchant_account_id"] = M2
    payout_snapshot["reservations"][0]["account_id"] = M2
    report = verify_snapshot(payout_snapshot)
    assert report.structure_ok and report.projection_ok
    assert not report.request_intent_ok
    assert "INVALID_PAYOUT_INTENT" in codes(report)


@pytest.mark.parametrize(
    "code,message",
    [
        ("INSUFFICIENT_AVAILABLE", "Insufficient available balance"),
        ("LIMIT_EXCEEDED", "Payout would exceed ledger limits"),
    ],
)
def test_retained_payout_rejection_has_no_effect_and_is_not_redecided(
    payout_snapshot, code, message
):
    body = {
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "operation_id": PAYOUT,
            "request_id": ATTEMPT,
        }
    }
    payout_snapshot["operations"][-1].update(
        admission="REJECTED",
        http_status=422,
        response_body=body,
    )
    payout_snapshot["requests"][-1].update(http_status=422, response_body=copy.deepcopy(body))
    for table in (
        "payouts",
        "reservations",
        "reservation_events",
        "dispatch_intents",
        "transport_attempts",
    ):
        payout_snapshot[table] = []
    payout_snapshot["balances"][2]["reserved"] = "0"
    # Today's80 can afford62; a retained historical rejection is still valid.
    assert verify_snapshot(payout_snapshot).healthy


def test_rejected_payout_cannot_retain_a_hold(payout_snapshot):
    payout_snapshot["operations"][-1].update(admission="REJECTED", http_status=422)
    report = verify_snapshot(payout_snapshot)
    assert not report.structure_ok and not report.request_intent_ok
    assert not report.rebuild_eligible


def test_four_digit_provider_year_is_required_even_on_strftime_platforms(payout_snapshot):
    payout = payout_snapshot["payouts"][0]
    provider = json.loads(bytes.fromhex(payout["provider_request_hex"]))
    provider["dispatch_deadline"] = "0001-01-01T00:00:00Z"
    raw = json.dumps(provider, sort_keys=True, separators=(",", ":")).encode()
    payout.update(
        dispatch_deadline="0001-01-01T00:00:00.000000Z",
        provider_request_hex=raw.hex(),
        provider_request_sha256=hashlib.sha256(raw).hexdigest(),
    )
    for row in (payout_snapshot["operations"][-1], payout_snapshot["requests"][-1]):
        row["response_body"]["dispatch_deadline"] = provider["dispatch_deadline"]
    assert verify_snapshot(payout_snapshot).healthy
    raw = raw.replace(b"0001-01-01", b"1-01-01")
    payout.update(
        provider_request_hex=raw.hex(), provider_request_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert not verify_snapshot(payout_snapshot).request_intent_ok


def add_attempt(data, outcome="MAY_HAVE_SENT"):
    data["payouts"][0].update(knowledge="UNKNOWN", version=2)
    data["dispatch_intents"][0].update(
        state="IN_FLIGHT",
        next_action="LOOKUP",
        attempts=1,
        last_attempt_id=ATTEMPT,
    )
    data["transport_attempts"] = [
        dict(
            workspace_id=W,
            attempt_id=ATTEMPT,
            payout_id=PAYOUT,
            action="PUT",
            started_at=STAMP,
            finished_at=None if outcome == "MAY_HAVE_SENT" else STAMP,
            outcome=outcome,
            http_status=None,
            response_payload_hex=None,
        )
    ]
    return data["transport_attempts"][0]


@pytest.mark.parametrize(
    "outcome", ["MAY_HAVE_SENT", "TIMEOUT", "CONNECTION_ERROR", "TRANSPORT_ERROR"]
)
def test_transport_absence_preserves_hold_and_never_claims_finality(payout_snapshot, outcome):
    add_attempt(payout_snapshot, outcome)
    report = verify_snapshot(payout_snapshot)
    assert report.healthy and "PAYOUT_EXTERNAL_RESULT" in report.unverified_intent_kinds
    assert payout_snapshot["balances"][2]["reserved"] == "62"


@pytest.mark.parametrize(
    "fault",
    ["missing-attempt", "counter", "wrong-last", "ready", "not-sent", "unbounded", "partial"],
)
def test_incoherent_dispatch_history_is_not_valid_source(payout_snapshot, fault):
    attempt = add_attempt(payout_snapshot)
    if fault == "missing-attempt":
        payout_snapshot["transport_attempts"] = []
    elif fault == "counter":
        payout_snapshot["dispatch_intents"][0]["attempts"] = 2
    elif fault == "wrong-last":
        payout_snapshot["dispatch_intents"][0]["last_attempt_id"] = BENEFICIARY
    elif fault == "ready":
        payout_snapshot["dispatch_intents"][0]["state"] = "READY"
    elif fault == "not-sent":
        payout_snapshot["payouts"][0]["knowledge"] = "NOT_SENT"
    elif fault == "unbounded":
        attempt.update(
            outcome="RESPONSE",
            finished_at=STAMP,
            http_status=200,
            response_payload_hex="00" * 65537,
        )
    else:
        attempt["finished_at"] = STAMP
    report = verify_snapshot(payout_snapshot)
    assert not report.structure_ok and not report.rebuild_eligible


def test_old_v4_keeps_nonzero_reservation_capability_boundary(snapshot):
    data = add_payout(snapshot)
    data["format_version"] = 4
    data["schema_versions"].pop()
    data["operations"].pop()
    data["requests"].pop()
    for name in (
        "payouts",
        "reservations",
        "reservation_events",
        "dispatch_intents",
        "transport_attempts",
    ):
        data.pop(name)
    report = verify_snapshot(data)
    assert report.healthy and report.projection_ok
    assert not report.reservation_supported and not report.rebuild_eligible
    assert "UNSUPPORTED_RESERVATION" in codes(report)


@pytest.mark.parametrize("fold_version", [1, 2])
def test_historical_repair_does_not_compare_old_reserve_with_current_holds(
    maintenance_snapshot, fold_version
):
    data = add_payout(maintenance_snapshot)
    data["repairs"][0]["fold_version"] = fold_version
    # Repair happened with zero holds; the new62 reservation came afterwards.
    assert data["repair_rows"][2]["after_reserved"] == "0"
    assert data["balances"][2]["reserved"] == "62"
    assert verify_snapshot(data).healthy


def test_new_repair_audits_reserved_values_but_legacy_fold_stays_zero(maintenance_snapshot):
    data = add_payout(maintenance_snapshot)
    data["repairs"][0]["fold_version"] = 2
    row = data["repair_rows"][2]
    row.update(before_reserved="61", after_reserved="62")
    assert verify_snapshot(data).healthy
    data["repairs"][0]["fold_version"] = 1
    assert not verify_snapshot(data).structure_ok


def for_export(data):
    data["server_version"] = "180006"
    for rows in data.values():
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    for key, value in row.items():
                        if key.endswith("_at") and value is not None:
                            row[key] = STAMP
    return data


def test_v5_roundtrip_preserves_instruction_and_raw_response_bytes(payout_snapshot, tmp_path):
    row = add_attempt(payout_snapshot, "RESPONSE")
    row.update(http_status=200, response_payload_hex=b"arbitrary retained response".hex())
    data = for_export(payout_snapshot)
    path = publish_snapshot(data, tmp_path, code_revision=REVISION)
    assert load_export(path) == data
    assert verify_snapshot(load_export(path)).healthy


@pytest.mark.parametrize(
    "fault", ["wrong-route", "http-bool", "hex", "message-bound", "future", "missing-table"]
)
def test_v5_loader_rejects_structurally_invalid_format(payout_snapshot, tmp_path, fault):
    data = for_export(payout_snapshot)
    if fault == "wrong-route":
        data["requests"][-1]["route_family"] = "payout:v2"
    elif fault == "http-bool":
        data["operations"][-1]["http_status"] = True
    elif fault == "hex":
        data["payouts"][0]["provider_request_hex"] = "gg"
    elif fault == "message-bound":
        data["payouts"][0]["provider_request_hex"] = "00" * 65537
    elif fault == "future":
        data["format_version"] = 6
    else:
        del data["reservation_events"]
    with pytest.raises(ExportError):
        publish_snapshot(data, tmp_path, code_revision=REVISION)
