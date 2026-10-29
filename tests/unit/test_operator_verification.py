"""Literal operator audit chains; deliberately independent of action builders."""

import copy
import hashlib
import json

import pytest
from test_payout_external_verification import finish, incident, observe, v6
from test_payout_verification import PAYOUT, REVISION, STAMP, W, for_export
from test_verification import codes
from test_verification import snapshot as snapshot

from ledgerops.export import ExportError, load_export, publish_snapshot
from ledgerops.verification import verify_snapshot

ACTION = "a0000000-0000-4000-8000-000000000001"
RUN = "a0000000-0000-4000-8000-000000000002"
MAKER = "a0000000-0000-4000-8000-000000000003"
CHECKER = "a0000000-0000-4000-8000-000000000004"
CLIENT = "a0000000-0000-4000-8000-000000000005"
EXECUTION = "a0000000-0000-4000-8000-000000000006"
GENERATION = "a0000000-0000-4000-8000-000000000007"
EXPIRES = "2026-10-29T10:01:00.000000Z"
TYPE = "REPROCESS_TERMINAL_LOCAL_BLOCKER"
TABLES = (
    "operator_actions",
    "operator_approvals",
    "operator_executions",
    "payout_incident_resolutions",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def retain(row, name, value):
    raw = canonical(value)
    row[name + "_hex"] = raw.hex()
    row[name + "_sha256"] = hashlib.sha256(raw).hexdigest()


def action_snapshot(data, state="SUCCEEDED", *, executed=True):
    v6(data)
    data["format_version"] = 9
    data["schema_versions"] += [
        "008_reconciliation_publication.sql",
        "009_authentication.sql",
        "010_operator_actions.sql",
    ]
    for name in TABLES:
        data[name] = []
    observation = observe(data, state)
    incident(data, observation, category="LOCAL_BLOCKER", code="PROJECTION_MISMATCH")
    fingerprints = [
        {
            "observation_id": observation["observation_id"],
            "envelope_sha256": observation["envelope_sha256"],
        }
    ]
    payout = {
        key: value for key, value in data["payouts"][0].items() if key != "provider_request_hex"
    }
    accounts = sorted(
        (
            copy.deepcopy(row)
            for row in data["accounts"]
            if row["class"] in {"MERCHANT_LIABILITY", "SYNTHETIC_CLEARING"}
            and row["id"] != "20000000-0000-4000-8000-000000000004"
        ),
        key=lambda row: row["id"],
    )
    # Keep the actual merchant plus the unique clearing; the fixture has two merchants.
    accounts = [
        row
        for row in accounts
        if row["class"] == "SYNTHETIC_CLEARING" or row["id"] == payout["merchant_account_id"]
    ]
    balances = sorted(
        (
            copy.deepcopy(row)
            for row in data["balances"]
            if row["account_id"] in {row["id"] for row in accounts}
        ),
        key=lambda row: row["account_id"],
    )
    intent = dict(
        action_type=TYPE,
        payout_id=PAYOUT,
        observation_id=observation["observation_id"],
        evidence_run_id=RUN,
        reason="Projection repaired; consume retained success",
        ttl_seconds=60,
    )
    proposal = dict(
        intent,
        workspace_id=W,
        action_id=ACTION,
        schema_version=1,
        created_at=STAMP,
        expires_at=EXPIRES,
        maker=dict(actor_id=MAKER, client_id=CLIENT, membership_version=1, client_version=1),
        auth_generation=GENERATION,
        evidence=dict(evidence_epoch=1, result_sha256="a" * 64, ledger_data_sha256="b" * 64),
        target=dict(
            projection_epoch=1,
            payout=payout,
            reservation=copy.deepcopy(data["reservations"][0]),
            accounts=accounts,
            balances=balances,
            observations=fingerprints,
            incidents=copy.deepcopy(data["payout_incidents"]),
            related_incidents=[],
        ),
        observation={
            key: observation[key] for key in ("observation_id", "envelope_sha256", "state")
        },
        observations=fingerprints,
        incident_ids=[data["payout_incidents"][0]["incident_id"]],
    )
    row = dict(
        workspace_id=W,
        action_id=ACTION,
        action_type=TYPE,
        payout_id=PAYOUT,
        observation_id=observation["observation_id"],
        evidence_run_id=RUN,
        maker_actor_id=MAKER,
        maker_client_id=CLIENT,
        maker_membership_version=1,
        maker_client_version=1,
        auth_generation=GENERATION,
        expires_at=EXPIRES,
        created_at=STAMP,
    )
    retain(row, "intent", intent)
    retain(row, "proposal", proposal)
    data["operator_actions"].append(row)
    data["operator_approvals"].append(
        dict(
            workspace_id=W,
            action_id=ACTION,
            proposal_sha256=row["proposal_sha256"],
            checker_actor_id=CHECKER,
            checker_client_id=CLIENT,
            checker_membership_version=1,
            checker_client_version=1,
            approved_at=STAMP,
        )
    )
    if executed:
        finish(data, observation)
        effect = data["payout_resolutions"][0]
        data["operator_executions"].append(
            dict(
                workspace_id=W,
                action_id=ACTION,
                execution_id=EXECUTION,
                proposal_sha256=row["proposal_sha256"],
                executor_actor_id=CHECKER,
                executor_client_id=CLIENT,
                observation_id=observation["observation_id"],
                state=state,
                journal_id=effect["journal_id"],
                http_status=200,
                response_body={
                    "action_id": ACTION,
                    "execution_id": EXECUTION,
                    "payout_id": PAYOUT,
                    "observation_id": observation["observation_id"],
                    "proposal_sha256": row["proposal_sha256"],
                    "state": state,
                    "journal_id": effect["journal_id"],
                    "resolved_incident_ids": proposal["incident_ids"],
                },
                executed_at=STAMP,
            )
        )
        data["payout_incident_resolutions"].append(
            dict(
                workspace_id=W,
                incident_id=data["payout_incidents"][0]["incident_id"],
                payout_id=PAYOUT,
                action_id=ACTION,
                observation_id=observation["observation_id"],
                resolved_at=STAMP,
            )
        )
    return data


@pytest.fixture
def audited(snapshot):
    return action_snapshot(for_export(snapshot))


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CLOSED_ABSENT"])
def test_executed_local_blocker_keeps_original_and_becomes_independently_healthy(snapshot, state):
    data = action_snapshot(snapshot, state)
    report = verify_snapshot(data)
    assert report.healthy, report.as_dict()
    assert report.external_evidence["local_blocker_payout_ids"] == []
    assert report.external_evidence["incident_payout_ids"] == []
    assert len(data["payout_incidents"]) == len(data["payout_incident_resolutions"]) == 1
    assert report.as_dict()["report_version"] == 3
    assert report.operator_actions["historical_authorization"] == "NOT_VERIFIED_NO_AUTH_HISTORY"
    assert report.external_evidence["signature_authenticity"] == "NOT_VERIFIED_NO_TRUST_KEYS"
    assert "OPERATOR_HISTORICAL_AUTHORIZATION" in report.unverified_intent_kinds


def test_pending_approval_retains_local_blocker(snapshot):
    data = action_snapshot(snapshot, executed=False)
    report = verify_snapshot(data)
    assert report.structure_ok
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]
    assert report.operator_actions["execution_count"] == 0


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda d: d["operator_actions"][0].update(proposal_sha256="0" * 64),
            "INVALID_OPERATOR_ACTION",
        ),
        (
            lambda d: d["operator_approvals"][0].update(checker_actor_id=MAKER),
            "INVALID_OPERATOR_APPROVAL",
        ),
        (
            lambda d: d["operator_approvals"][0].update(proposal_sha256="0" * 64),
            "INVALID_OPERATOR_APPROVAL",
        ),
        (lambda d: d["operator_executions"].clear(), "INVALID_INCIDENT_DISPOSITION"),
        (
            lambda d: d["operator_executions"][0].update(observation_id=RUN),
            "INVALID_OPERATOR_EXECUTION",
        ),
        (
            lambda d: d["operator_executions"][0].update(executed_at=EXPIRES),
            "INVALID_OPERATOR_EXECUTION",
        ),
        (
            lambda d: d["payout_incident_resolutions"][0].update(action_id=RUN),
            "INVALID_INCIDENT_DISPOSITION",
        ),
        (lambda d: d["payout_incident_resolutions"].clear(), "INCOMPLETE_ACTION_DISPOSITIONS"),
    ],
)
def test_corrupt_audit_never_hides_the_original_blocker(audited, mutation, expected):
    mutation(audited)
    report = verify_snapshot(audited)
    assert not report.structure_ok and not report.healthy
    assert expected in codes(report)
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]


def test_proposal_fingerprint_tampering_survives_checksum_recompute_but_fails_binding(audited):
    row = audited["operator_actions"][0]
    proposal = json.loads(bytes.fromhex(row["proposal_hex"]))
    proposal["observations"][0]["envelope_sha256"] = "0" * 64
    retain(row, "proposal", proposal)
    report = verify_snapshot(audited)
    assert "INVALID_OPERATOR_ACTION" in codes(report)
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]


def test_additional_unresolved_local_blocker_is_not_swallowed(audited):
    observation = audited["provider_observations"][0]
    for _ in range(33):
        incident(audited, observation, category="LOCAL_BLOCKER", code="PROJECTION_MISMATCH")
    report = verify_snapshot(audited)
    assert report.structure_ok
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]


def test_nonlocal_incident_disposition_is_rejected(audited):
    audited["payout_incidents"][0].update(
        category="EVIDENCE_LIMIT", code="EVIDENCE_CAPACITY", related_payload_sha256="0" * 64
    )
    report = verify_snapshot(audited)
    assert not report.structure_ok
    assert "INVALID_INCIDENT_DISPOSITION" in codes(report)
    assert report.external_evidence["local_blocker_payout_ids"] == [PAYOUT]


def test_v9_round_trip_without_auth_secrets(audited, tmp_path):
    path = publish_snapshot(audited, tmp_path, code_revision=REVISION, dirty=False)
    loaded = load_export(path)
    assert verify_snapshot(loaded).healthy
    assert loaded["format_version"] == 9
    assert not any(key.startswith("auth_") for key in loaded)


def test_v8_cannot_claim_v9_dispositions(audited, tmp_path):
    audited["format_version"] = 8
    audited["schema_versions"].pop()
    with pytest.raises(ExportError):
        publish_snapshot(audited, tmp_path, code_revision=REVISION, dirty=False)


def test_v9_saved_resume_report_retains_explicit_unverified_authorization(audited):
    saved = verify_snapshot(audited).as_dict()
    audited["maintenance_freezes"].append(
        {
            "workspace_id": W,
            "freeze_id": RUN,
            "actor_id": MAKER,
            "reason": "Historical audited freeze",
            "frozen_at": STAMP,
            "resumed_at": STAMP,
            "resumed_by": CHECKER,
            "resume_reason": "Fresh A and structure checks passed",
            "resume_report": saved,
        }
    )
    assert verify_snapshot(audited).healthy
    audited["maintenance_freezes"][0]["resume_report"]["operator_actions"][
        "historical_authorization"
    ] = "VERIFIED"
    report = verify_snapshot(audited)
    assert not report.structure_ok
    assert "INVALID_FREEZE" in codes(report)


def test_old_v8_keeps_report_v2_without_operator_claims(audited):
    audited["format_version"] = 8
    audited["schema_versions"].pop()
    for name in TABLES:
        audited.pop(name)
    report = verify_snapshot(audited)
    assert report.structure_ok
    assert report.as_dict()["report_version"] == 2
    assert "operator_actions" not in report.as_dict()
    assert "OPERATOR_HISTORICAL_AUTHORIZATION" not in report.unverified_intent_kinds
