"""Pure capability checks for complete saved barrier reviews; legacy reports stay readable."""

import copy

import pytest

from ledgerops.maintenance_snapshot import CAPTURE_CONTEXT
from ledgerops.stream_verification import verify_stream
from ledgerops.verification import _Invalid, _resume_report, verify_snapshot
from tests.unit.stream_fixture_helpers import source, write
from tests.unit.test_verification import snapshot as snapshot


@pytest.fixture
def review(snapshot, tmp_path):
    report = verify_stream(write(source(snapshot), tmp_path / "source"))
    assert report["healthy"] and report["verification_complete"]
    report.update(
        capture_context=CAPTURE_CONTEXT, source_export_id=None, source_manifest_sha256=None
    )
    return report


def test_complete_saved_barrier_review_preserves_a_only_policy(review):
    _resume_report(review)
    warning = copy.deepcopy(review)
    warning.update(
        request_intent_ok=False,
        healthy=False,
        rebuild_eligible=False,
        issues=[
            {
                "code": "WRONG_OWNER",
                "object_id": "purchase",
                "field": "owner",
                "expected": "M1",
                "actual": "M2",
            }
        ],
        issue_count=1,
    )
    _resume_report(warning)


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "context",
        "missing_context",
        "engine",
        "format",
        "bool_complete",
        "incomplete",
        "unverified",
        "limits",
        "bool_limit",
        "claim",
        "bad_hash",
        "invented_manifest",
        "derived",
        "scope",
        "count",
        "bool_count",
        "hidden_issue",
        "extra",
    ],
)
def test_saved_v4_unknown_or_incomplete_capabilities_are_not_legacy(review, fault):
    bad = copy.deepcopy(review)
    if fault == "version":
        bad["report_version"] = 5
    elif fault == "context":
        bad["capture_context"] = "FUTURE_CAPTURE"
    elif fault == "missing_context":
        del bad["capture_context"]
    elif fault == "engine":
        bad["verification_engine"] = "FUTURE_CHECKER"
    elif fault == "format":
        bad["format_version"] = True
    elif fault == "bool_complete":
        bad["verification_complete"] = 1
    elif fault == "incomplete":
        bad["verification_complete"] = False
    elif fault == "unverified":
        bad["unverified_scope_count"] = 1
    elif fault == "limits":
        bad["limits"]["semantic_closure_bytes"] *= 2
    elif fault == "bool_limit":
        bad["limits"]["sqlite_cache_bytes"] = True
    elif fault == "claim":
        bad["external_evidence"]["signature_authenticity"] = "VERIFIED"
    elif fault == "bad_hash":
        bad["source_content_sha256"] = "bad"
    elif fault == "invented_manifest":
        bad["source_manifest_sha256"] = "0" * 64
    elif fault == "derived":
        bad["healthy"] = False
    elif fault == "scope":
        bad["unverified_intent_kinds"] = ["OPENING"]
    elif fault == "count":
        bad["external_evidence"]["resolved_payout_count"] = 1
    elif fault == "bool_count":
        bad["issue_count"] = False
    elif fault == "hidden_issue":
        bad.update(issue_count=1, issues_truncated=True)
    else:
        bad["future_capability"] = True
    with pytest.raises(_Invalid):
        _resume_report(bad)


def test_nested_saved_barrier_review_is_checked_by_legacy_full_verifier(snapshot, review):
    data = source(snapshot)
    data["maintenance_freezes"] = [
        {
            "workspace_id": data["workspace"]["id"],
            "freeze_id": "90000000-0000-4000-8000-000000000001",
            "actor_id": data["accounts"][0]["owner_id"],
            "reason": "review",
            "frozen_at": "2026-10-29T10:00:00+00:00",
            "resumed_at": "2026-10-29T10:01:00+00:00",
            "resumed_by": data["accounts"][0]["owner_id"],
            "resume_reason": "checked",
            "resume_report": review,
        }
    ]
    assert verify_snapshot(data).structure_ok
    data["maintenance_freezes"][0]["resume_report"]["capture_context"] = "UNKNOWN"
    report = verify_snapshot(data)
    assert not report.structure_ok and "INVALID_FREEZE" in {issue.code for issue in report.issues}
