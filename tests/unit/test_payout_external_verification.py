"""Literal v6 observations and financial effects, independent of live resolver/signing code."""

import copy
import hashlib
import json

import pytest
from test_payout_verification import (
    M1,
    M2,
    PAYOUT,
    REVISION,
    STAMP,
    W,
    add_payout,
    for_export,
)
from test_verification import CLEARING, bytes_fields, codes
from test_verification import maintenance_snapshot as maintenance_snapshot
from test_verification import snapshot as snapshot

from ledgerops.export import ExportError, load_export, publish_snapshot
from ledgerops.verification import verify_snapshot

JOURNAL = "50000000-0000-4000-8000-000000000003"
TABLES = (
    "provider_observations",
    "observation_receipts",
    "observation_quarantines",
    "payout_incidents",
    "payout_resolutions",
    "related_incidents",
)


def v6(data):
    add_payout(data)
    data["format_version"] = 6
    data["schema_versions"].append("007_payout_resolution.sql")
    data["payouts"][0]["source_sequence"] = 0
    for table in TABLES:
        data[table] = []
    return data


@pytest.fixture
def pending(snapshot):
    return v6(snapshot)


def observe(
    data, state="SUCCEEDED", sequence=1, *, event_id=None, payout_id=PAYOUT, kind="execution"
):
    index = len(data["provider_observations"]) + 1
    observation_id = f"90000000-0000-4000-8000-{index:012d}"
    event_id = event_id or f"91000000-0000-4000-8000-{index:012d}"
    payout = next(row for row in data["payouts"] if row["operation_id"] == payout_id)
    request = bytes.fromhex(payout["provider_request_hex"])
    value = dict(
        kind=kind,
        issuer="ledgerops-provider-sim",
        contract_version=1,
        namespace=payout["provider_namespace"],
        provider_key=payout["provider_key"],
        state=state,
    )
    if kind == "execution":
        value.update(
            event_id=event_id,
            source_sequence=sequence,
            request_sha256=hashlib.sha256(request).hexdigest(),
            request=json.loads(request),
        )
    else:
        value.update(observation_id=event_id, visibility_watermark=sequence)
    signed = json.dumps(value, sort_keys=True, separators=(",", ":"))
    envelope = {"key_id": "test-v1", "payload": signed, "signature": "0" * 64}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    row = dict(
        workspace_id=W,
        payout_id=payout_id,
        observation_id=observation_id,
        provider_namespace=payout["provider_namespace"],
        event_id=event_id,
        kind=kind,
        state=state,
        source_sequence=sequence,
        key_id="test-v1",
        signed_payload=signed,
        signature="0" * 64,
        envelope_hex=raw.hex(),
        envelope_sha256=hashlib.sha256(raw).hexdigest(),
        received_at=STAMP,
    )
    data["provider_observations"].append(row)
    data["observation_receipts"].append(
        dict(
            workspace_id=W,
            receipt_id=f"92000000-0000-4000-8000-{index:012d}",
            payout_id=payout_id,
            observation_id=observation_id,
            channel="CALLBACK",
            transport_attempt_id=None,
            received_at=STAMP,
        )
    )
    return row


def finish(data, observation):
    state = observation["state"]
    data["payouts"][0].update(
        knowledge=state, source_sequence=observation["source_sequence"], version=2
    )
    hold_state = "CONSUMED" if state == "SUCCEEDED" else "RELEASED"
    data["reservations"][0].update(state=hold_state, version=2)
    data["reservation_events"].append(
        dict(
            workspace_id=W,
            payout_id=PAYOUT,
            event_no=2,
            state=hold_state,
            observation_id=observation["observation_id"],
            recorded_at=STAMP,
        )
    )
    data["dispatch_intents"][0]["state"] = "DONE"
    data["payout_resolutions"].append(
        dict(
            workspace_id=W,
            payout_id=PAYOUT,
            observation_id=observation["observation_id"],
            state=state,
            journal_id=JOURNAL if state == "SUCCEEDED" else None,
            resolved_at=STAMP,
        )
    )
    data["balances"][2]["reserved"] = "0"
    data["balances"][2]["version"] += 1
    if state == "SUCCEEDED":
        data["journals"].append(
            dict(
                workspace_id=W,
                journal_id=JOURNAL,
                operation_id=PAYOUT,
                kind="PAYOUT",
                line_count=2,
                total_minor="62",
                recorded_at=STAMP,
            )
        )
        for number, account, side in [(1, M1, "D"), (2, CLEARING, "C")]:
            data["entries"].append(
                dict(
                    workspace_id=W,
                    journal_id=JOURNAL,
                    line_no=number,
                    account_id=account,
                    side=side,
                    amount_minor="62",
                )
            )
        data["balances"][0].update(credits="62", booked="38", version=2)
        data["balances"][2].update(debits="62", booked="18")
    return data


def incident(
    data, incoming, related=None, category="PROVIDER_CONTRADICTION", code="TERMINAL_CONTRADICTION"
):
    index = len(data["payout_incidents"]) + 1
    data["payout_incidents"].append(
        dict(
            workspace_id=W,
            incident_id=f"93000000-0000-4000-8000-{index:012d}",
            payout_id=incoming["payout_id"],
            category=category,
            code=code,
            observation_id=incoming["observation_id"],
            related_observation_id=None if related is None else related["observation_id"],
            related_payload_sha256=None if related is None else related["envelope_sha256"],
            created_at=STAMP,
        )
    )


def rewrite_envelope(row, change):
    value = json.loads(row["signed_payload"])
    change(value)
    row["signed_payload"] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    envelope = dict(key_id=row["key_id"], payload=row["signed_payload"], signature=row["signature"])
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    row.update(envelope_hex=raw.hex(), envelope_sha256=hashlib.sha256(raw).hexdigest())


def test_pending_healthy_means_internal_consistency_not_provider_finality(pending):
    report = verify_snapshot(pending)
    assert report.healthy and report.rebuild_eligible
    external = report.as_dict()["external_evidence"]
    assert report.as_dict()["report_version"] == 2
    assert external["signature_authenticity"] == "NOT_VERIFIED_NO_TRUST_KEYS"
    assert external["independent_provider_history"] == "NOT_PROVIDED"
    assert external["payouts_resolved"] is False and external["unresolved_payout_ids"] == [PAYOUT]
    assert report.unverified_intent_kinds == (
        "OPENING",
        "PAYOUT_EXTERNAL_EFFECT",
        "PAYOUT_PROVIDER_AUTHENTICITY",
    )


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CLOSED_ABSENT"])
def test_terminal_facts_match_hold_journal_and_original_202(pending, state):
    original = copy.deepcopy(pending["operations"][-1])
    finish(pending, observe(pending, state))
    report = verify_snapshot(pending)
    assert report.healthy and report.rebuild_eligible
    assert report.external_evidence["payouts_resolved"] is True
    assert report.external_evidence["resolved_payout_count"] == 1
    assert pending["operations"][-1] == original
    assert pending["balances"][2]["reserved"] == "0"


def test_old_nonterminal_and_compatible_terminal_do_not_require_new_financial_effect(pending):
    finish(pending, observe(pending, "SUCCEEDED", 10))
    observe(pending, "ACCEPTED", 2)
    observe(pending, "SUCCEEDED", 12)
    report = verify_snapshot(pending)
    assert report.healthy and report.external_evidence["retained_observation_count"] == 3
    assert len(pending["payout_resolutions"]) == 1


def test_lookup_absence_has_no_money_fields_and_is_not_terminal(pending):
    observe(pending, "NOT_FOUND", 50, kind="lookup")
    report = verify_snapshot(pending)
    assert report.healthy and not report.external_evidence["payouts_resolved"]
    assert pending["balances"][2]["reserved"] == "62"


def test_accepted_callback_can_precede_local_attempt_history(pending):
    observe(pending, "ACCEPTED", 4)
    pending["payouts"][0].update(knowledge="ACCEPTED", source_sequence=4, version=2)
    pending["dispatch_intents"][0]["state"] = "OBSERVING"
    assert verify_snapshot(pending).healthy


@pytest.mark.parametrize(
    "first,second", [("SUCCEEDED", "FAILED"), ("FAILED", "SUCCEEDED"), ("FAILED", "CLOSED_ABSENT")]
)
def test_stale_contradictory_terminal_remains_incident_without_reversing_money(
    pending, first, second
):
    original = observe(pending, first, 10)
    finish(pending, original)
    before = copy.deepcopy(pending["balances"])
    incoming = observe(pending, second, 2)
    incident(pending, incoming, original)
    report = verify_snapshot(pending)
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert not report.healthy and report.rebuild_eligible
    assert report.external_evidence["external_consistency_ok"] is False
    assert report.external_evidence["incident_payout_ids"] == [PAYOUT]
    assert pending["balances"] == before and pending["dispatch_intents"][0]["state"] == "DONE"


def test_newer_nonterminal_after_final_is_a_provider_contradiction(pending):
    terminal = observe(pending, "SUCCEEDED", 1)
    finish(pending, terminal)
    incoming = observe(pending, "ACCEPTED", 2)
    incident(pending, incoming, terminal, code="NONTERMINAL_AFTER_FINAL")
    report = verify_snapshot(pending)
    assert report.structure_ok and not report.healthy
    assert "NONTERMINAL_AFTER_FINAL" in codes(report)


def test_same_event_id_changed_bytes_requires_retained_incident(pending):
    first = observe(pending, "ACCEPTED", 1)
    pending["payouts"][0].update(knowledge="ACCEPTED", source_sequence=1, version=2)
    pending["dispatch_intents"][0]["state"] = "BLOCKED"
    second = observe(pending, "SUCCEEDED", 1, event_id=first["event_id"])
    incident(pending, second, first, code="EVENT_ID_REUSED")
    report = verify_snapshot(pending)
    assert report.structure_ok and report.request_intent_ok and not report.healthy
    pending["payout_incidents"] = []
    assert not verify_snapshot(pending).structure_ok


def test_local_capacity_blocker_preserves_hold_and_is_explicitly_unresolved(pending):
    success = observe(pending)
    incident(pending, success, category="LOCAL_BLOCKER", code="CAPACITY_EXCEEDED")
    pending["dispatch_intents"][0]["state"] = "BLOCKED"
    report = verify_snapshot(pending)
    assert report.healthy and report.rebuild_eligible
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]
    assert not report.external_evidence["payouts_resolved"]


def test_related_cross_workspace_incident_contains_only_local_identity_and_digest(pending):
    observation = observe(pending, "ACCEPTED", 1)
    pending["payouts"][0].update(knowledge="ACCEPTED", source_sequence=1, version=2)
    pending["dispatch_intents"][0]["state"] = "OBSERVING"
    pending["related_incidents"] = [
        dict(
            workspace_id=W,
            incident_id="93000000-0000-4000-8000-000000000099",
            payout_id=PAYOUT,
            observation_id=observation["observation_id"],
            code="EVENT_ID_REUSED",
            related_payload_sha256="f" * 64,
            created_at=STAMP,
        )
    ]
    report = verify_snapshot(pending)
    assert report.structure_ok and report.request_intent_ok and not report.healthy
    assert report.external_evidence["incident_payout_ids"] == [PAYOUT]
    assert report.rebuild_eligible


def test_invalid_quarantine_is_visible_but_cannot_stop_valid_payout(pending):
    raw = b"unauthenticated nonsense"
    pending["observation_quarantines"] = [
        dict(
            workspace_id=W,
            quarantine_id="94000000-0000-4000-8000-000000000001",
            payout_id=PAYOUT,
            channel="CALLBACK",
            reason="INVALID_OBSERVATION",
            raw_payload_hex=raw.hex(),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            received_at=STAMP,
        )
    ]
    report = verify_snapshot(pending)
    assert report.healthy and report.external_evidence["quarantine_count"] == 1
    assert report.external_evidence["incident_payout_ids"] == []
    assert "QUARANTINED_OBSERVATIONS" in codes(report)


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "key",
        "namespace",
        "request",
        "amount",
        "sequence",
        "state",
        "signature-shape",
        "duplicate-json",
        "envelope-columns",
    ],
)
def test_malformed_observation_never_authorizes_a_financial_effect(pending, fault):
    row = observe(pending)
    finish(pending, row)
    if fault == "digest":
        row["envelope_sha256"] = "f" * 64
    elif fault == "key":
        rewrite_envelope(row, lambda value: value.update(provider_key="another"))
    elif fault == "namespace":
        rewrite_envelope(row, lambda value: value.update(namespace=W))
    elif fault == "request":
        rewrite_envelope(row, lambda value: value["request"].update(beneficiary_id=W))
    elif fault == "amount":
        rewrite_envelope(row, lambda value: value["request"].update(gross_minor="61"))
    elif fault == "sequence":
        row["source_sequence"] = True
    elif fault == "state":
        row["state"] = "FAILED"
    elif fault == "signature-shape":
        row["signature"] = "no"
    elif fault == "duplicate-json":
        row["signed_payload"] = '{"kind":"execution","kind":"lookup"}'
    else:
        row["signed_payload"] += " "
    report = verify_snapshot(pending)
    assert not report.structure_ok and not report.request_intent_ok and not report.rebuild_eligible


def test_signature_bytes_are_not_claimed_authenticated_without_keys(pending):
    row = observe(pending)
    row["signature"] = "a" * 64
    rewrite_envelope(row, lambda _: None)
    finish(pending, row)
    report = verify_snapshot(pending)
    assert report.healthy
    assert report.external_evidence["signature_authenticity"] == "NOT_VERIFIED_NO_TRUST_KEYS"


@pytest.mark.parametrize(
    "fault",
    [
        "missing-resolution",
        "wrong-observation",
        "extra-terminal",
        "wrong-hold-state",
        "wrong-reserved",
        "wrong-knowledge",
        "wrong-sequence",
        "missing-journal",
        "wrong-amount",
        "wrong-owner",
        "missing-receipt",
        "done-without-resolution",
    ],
)
def test_terminal_lifecycle_and_local_effect_defects_are_detected(pending, fault):
    row = observe(pending)
    finish(pending, row)
    if fault == "missing-resolution":
        pending["payout_resolutions"] = []
    elif fault == "wrong-observation":
        pending["reservation_events"][-1]["observation_id"] = W
    elif fault == "extra-terminal":
        pending["reservation_events"].append(copy.deepcopy(pending["reservation_events"][-1]))
    elif fault == "wrong-hold-state":
        pending["reservations"][0]["state"] = "RELEASED"
    elif fault == "wrong-reserved":
        pending["balances"][2]["reserved"] = "1"
    elif fault == "wrong-knowledge":
        pending["payouts"][0]["knowledge"] = "FAILED"
    elif fault == "wrong-sequence":
        pending["payouts"][0]["source_sequence"] = 100
    elif fault == "missing-journal":
        pending["journals"].pop()
        pending["entries"] = pending["entries"][:-2]
    elif fault == "wrong-amount":
        pending["entries"][-1]["amount_minor"] = "61"
    elif fault == "wrong-owner":
        pending["entries"][-2]["account_id"] = M2
    elif fault == "missing-receipt":
        pending["observation_receipts"] = []
    else:
        pending["payout_resolutions"] = []
        pending["reservation_events"].pop()
        pending["reservations"][0].update(state="HELD", version=1)
    report = verify_snapshot(pending)
    assert not report.healthy


def test_historical_reserved_repair_remains_valid_after_later_consumption(maintenance_snapshot):
    data = v6(maintenance_snapshot)
    data["repairs"][0]["fold_version"] = 2
    data["repair_rows"][2].update(before_reserved="61", after_reserved="62")
    finish(data, observe(data))
    assert verify_snapshot(data).healthy


def test_v6_bundle_roundtrip_retains_raw_evidence_and_explicit_coverage(pending, tmp_path):
    finish(pending, observe(pending))
    data = for_export(pending)
    directory = publish_snapshot(data, tmp_path, code_revision=REVISION)
    assert load_export(directory) == data
    assert verify_snapshot(load_export(directory)).as_dict()["report_version"] == 2


def test_v6_export_does_not_allow_new_observation_kind_to_masquerade_as_journal_kind(
    pending, tmp_path
):
    observe(pending)
    pending["provider_observations"][0]["kind"] = "PAYOUT"
    with pytest.raises(ExportError):
        publish_snapshot(for_export(pending), tmp_path, code_revision=REVISION)


def _make_two_smaller_payouts(data):
    payout2 = "40000000-0000-4000-8000-000000000004"
    operation = data["operations"][-1]
    captured = json.loads(bytes.fromhex(operation["payload_hex"]).split(b"\n", 1)[1])
    captured["principal_minor"] = "30"
    raw = b"ledgerops.payout:v1\n" + json.dumps(captured, separators=(",", ":")).encode()
    for row in (operation, data["requests"][-1]):
        row.update(bytes_fields(raw))
        row["response_body"].update(principal_minor="30", gross_minor="32")
    payout = data["payouts"][0]
    request = json.loads(bytes.fromhex(payout["provider_request_hex"]))
    request.update(principal_minor="30", gross_minor="32")
    raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    payout.update(
        principal_minor="30",
        gross_minor="32",
        provider_request_hex=raw.hex(),
        provider_request_sha256=hashlib.sha256(raw).hexdigest(),
    )
    data["reservations"][0]["gross_minor"] = "32"
    for table in (
        "operations",
        "requests",
        "payouts",
        "reservations",
        "reservation_events",
        "dispatch_intents",
    ):
        row = copy.deepcopy(data[table][-1])
        for key in ("operation_id", "payout_id"):
            if key in row:
                row[key] = payout2
        if table in {"operations", "requests"}:
            captured["operation_id"] = payout2
            raw = b"ledgerops.payout:v1\n" + json.dumps(captured, separators=(",", ":")).encode()
            row.update(bytes_fields(raw))
            row["response_body"].update(operation_id=payout2, payout_id=payout2)
            if table == "requests":
                row["key"] = "second-payout"
        elif table == "payouts":
            request["operation_id"] = payout2
            raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            row.update(
                provider_key=row["provider_key"].replace(PAYOUT, payout2),
                provider_request_hex=raw.hex(),
                provider_request_sha256=hashlib.sha256(raw).hexdigest(),
            )
        data[table].append(row)
    data["balances"][2]["reserved"] = "64"
    return payout2


def test_same_workspace_identity_collision_marks_original_and_incoming_payouts(pending):
    payout2 = _make_two_smaller_payouts(pending)
    first = observe(pending, "ACCEPTED", 1)
    pending["payouts"][0].update(knowledge="ACCEPTED", source_sequence=1, version=2)
    pending["dispatch_intents"][0]["state"] = "OBSERVING"
    second = observe(pending, "ACCEPTED", 1, event_id=first["event_id"], payout_id=payout2)
    pending["dispatch_intents"][1]["state"] = "BLOCKED"
    incident(pending, second, first, code="EVENT_ID_REUSED")
    report = verify_snapshot(pending)
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert not report.healthy and report.rebuild_eligible
    assert report.external_evidence["incident_payout_ids"] == [PAYOUT, payout2]


def _close_with_report(data, report):
    data["workspace"].update(mode="NORMAL", current_freeze_id=None)
    data["maintenance_freezes"][0].update(
        resumed_at=STAMP, resumed_by=W, resume_reason="checked", resume_report=copy.deepcopy(report)
    )


def test_report_v2_roundtrip_does_not_deadlock_resume_on_provider_incident(maintenance_snapshot):
    data = v6(maintenance_snapshot)
    original = observe(data, "SUCCEEDED", 10)
    finish(data, original)
    incoming = observe(data, "FAILED", 2)
    incident(data, incoming, original)
    report = verify_snapshot(data)
    assert report.structure_ok and report.projection_ok and not report.healthy
    _close_with_report(data, report.as_dict())
    resumed = verify_snapshot(data)
    assert resumed.structure_ok and resumed.projection_ok and not resumed.healthy


def test_legacy_saved_report_stays_readable_inside_v6_snapshot(maintenance_snapshot):
    data = v6(maintenance_snapshot)
    report = verify_snapshot(data).as_dict()
    del report["report_version"], report["external_evidence"]
    report["unverified_intent_kinds"] = ["OPENING", "PAYOUT_EXTERNAL_RESULT"]
    _close_with_report(data, report)
    assert verify_snapshot(data).healthy


@pytest.mark.parametrize(
    "fault",
    [
        "signature-claim",
        "unknown-version",
        "count",
        "duplicates",
        "missing-field",
        "derived-health",
    ],
)
def test_saved_report_v2_rejects_false_coverage_and_inconsistent_counts(
    maintenance_snapshot, fault
):
    data = v6(maintenance_snapshot)
    report = verify_snapshot(data).as_dict()
    if fault == "signature-claim":
        report["external_evidence"]["signature_authenticity"] = "VERIFIED"
    elif fault == "unknown-version":
        report["report_version"] = True
    elif fault == "count":
        report["external_evidence"]["resolved_payout_count"] = 1
    elif fault == "duplicates":
        report["external_evidence"]["unresolved_payout_ids"] *= 2
    elif fault == "missing-field":
        del report["external_evidence"]["independent_provider_history"]
    else:
        report["healthy"] = False
    _close_with_report(data, report)
    assert not verify_snapshot(data).structure_ok


def test_authenticated_terminal_record_without_application_or_blocker_is_not_healthy(pending):
    observe(pending, "SUCCEEDED")
    report = verify_snapshot(pending)
    assert not report.request_intent_ok and not report.rebuild_eligible
    assert "MISSING_TERMINAL_RESOLUTION" in codes(report)


def test_retained_accepted_record_cannot_silently_leave_not_sent_knowledge(pending):
    observe(pending, "ACCEPTED")
    report = verify_snapshot(pending)
    assert not report.structure_ok and not report.healthy
