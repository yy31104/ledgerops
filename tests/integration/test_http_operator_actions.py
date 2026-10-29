"""Real HTTP maker/checker flow uses current database identities and immutable bytes."""

import hashlib
import json
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo

from ledgerops import authentication as auth
from tests.integration.http_helpers import ProcessService
from tests.integration.payout_helpers import money
from tests.integration.purchase_helpers import purchase_input
from tests.integration.test_authentication import ADMIN, provision
from tests.integration.test_operator_actions import SCOPES, operator_story
from tests.provider.test_process import SECRET

pytestmark = pytest.mark.integration


def token(db, credential):
    with db.connect("auth_admin") as conn:
        return auth.issue_credential(
            conn,
            str(credential.principal.workspace_id),
            str(credential.principal.client_id),
            SCOPES,
            admin_actor_id=ADMIN,
            reason="HTTP operator test",
        ).token


@pytest.fixture
def operator_service(seeded_db, golden, tmp_path):
    story = operator_story(seeded_db, golden, tmp_path)
    keys = tmp_path / "operator-keys.json"
    keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
    keys.chmod(0o600)
    dsn = make_conninfo(
        host="127.0.0.1",
        port=seeded_db.port,
        dbname=seeded_db.name,
        user="ledger_action_operator",
        password=seeded_db.operator_password,
    )
    service = ProcessService(
        seeded_db,
        tmp_path / "operator-http",
        LEDGEROPS_OPERATOR_DSN=dsn,
        LEDGEROPS_OPERATOR_OBJECTS=str(story["store"].root),
        LEDGEROPS_OPERATOR_NAMESPACE=str(story["trust"].namespace),
        LEDGEROPS_OPERATOR_KEYS_FILE=str(keys),
    )
    try:
        yield story, service, token(seeded_db, story["maker"]), token(seeded_db, story["checker"])
    finally:
        service.close()


def propose_body(story):
    return {
        "action_id": str(uuid4()),
        "action_type": "REPROCESS_TERMINAL_LOCAL_BLOCKER",
        "payout_id": str(story["payout_id"]),
        "observation_id": str(story["observation_id"]),
        "evidence_run_id": str(story["evidence_run_id"]),
        "reason": "修复后重新核验原终局证据",
        "ttl_seconds": 900,
    }


def test_actual_http_distinct_review_execution_and_exact_proposal_bytes(operator_service):
    story, service, maker, checker = operator_service
    assert service.request("GET", "/health/ready")[0] == 200
    status, raw, _ = service.request("GET", "/v1/me", token=maker)
    assert status == 200 and json.loads(raw)["operator_controls_available"]
    assert service.request("GET", "/v1/accounts", token=maker)[0] == 403
    request = propose_body(story)
    status, raw, _ = service.request("POST", "/v1/operator/actions", request, token=maker)
    proposal = json.loads(raw)
    assert status == 201, raw
    assert "proposal" not in proposal
    assert (
        hashlib.sha256(proposal["proposal_json"].encode()).hexdigest()
        == proposal["proposal_sha256"]
    )
    assert proposal["review"]["gross_minor"] == "62"
    status, raw, _ = service.request(
        "GET", "/v1/investigation/operations/" + request["payout_id"], token=maker
    )
    references = json.loads(raw)["current_reconciliation_references"]
    assert status == 200 and references[0]["run_id"] == request["evidence_run_id"]
    assert type(references[0]["workspace_evidence_epoch"]) is str
    path = "/v1/operator/actions/" + request["action_id"]
    review = {"proposal_sha256": proposal["proposal_sha256"]}
    assert service.request("POST", path + "/approve", review, token=maker)[0] == 403
    assert service.request("POST", path + "/approve", review, token=checker)[0] == 201
    assert service.request("POST", path + "/approve", review, token=checker)[0] == 200
    execute = {**review, "execution_id": str(uuid4())}
    status, raw, _ = service.request("POST", path + "/execute", execute, token=checker)
    result = json.loads(raw)
    assert status == 200 and result["state"] == "SUCCEEDED", raw
    assert money(story["database"], story["golden"])[:3] == (18, 0, 18)
    assert service.request("POST", path + "/execute", execute, token=checker)[:2] == (200, raw)
    status, raw, _ = service.request("GET", path, token=checker)
    history = json.loads(raw)
    assert status == 200 and history["execution"]["journal_id"] == result["journal_id"]
    assert history["attempts"][0]["execution_id"] == execute["execution_id"]
    assert maker not in service.logs() and checker not in service.logs()


def test_operator_money_override_or_unknown_action_type_never_gets_admitted(operator_service):
    story, service, maker, _ = operator_service
    for changes in ({"action_type": "RELEASE_UNKNOWN"}, {"gross_minor": "1"}):
        status, _, _ = service.request(
            "POST", "/v1/operator/actions", {**propose_body(story), **changes}, token=maker
        )
        assert status == 400
    with story["database"].connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.operator_action").fetchone() == (0,)
    assert money(story["database"], story["golden"])[:3] == (80, 62, 18)


def test_public_instance_has_no_operator_control_plane(seeded_db, golden, tmp_path):
    service = ProcessService(seeded_db, tmp_path / "public")
    try:
        principal, _, _ = purchase_input(golden)
        issued = provision(seeded_db, principal, scopes=["evidence:read"])
        status, raw, _ = service.request("GET", "/v1/me", token=issued.token)
        assert status == 200 and not json.loads(raw)["operator_controls_available"]
        assert service.request("POST", "/v1/operator/actions", {}, token=issued.token)[0] == 404
    finally:
        service.close()


def test_physical_restore_blocks_operator_writes_but_preserves_audit_read(operator_service):
    story, service, maker, _ = operator_service
    request = propose_body(story)
    assert service.request("POST", "/v1/operator/actions", request, token=maker)[0] == 201
    frozen = ProcessService(
        story["database"],
        service.stdout.parent / "restore-instance",
        **{
            name: value
            for name, value in service.environment.items()
            if name.startswith("LEDGEROPS_OPERATOR_")
        },
        LEDGEROPS_RESTORE_MODE="true",
    )
    try:
        path = "/v1/operator/actions/" + request["action_id"]
        assert frozen.request("GET", path, token=maker)[0] == 200
        assert frozen.request("POST", "/v1/operator/actions", request, token=maker)[0] == 503
        assert frozen.request("POST", path + "/approve", {}, token=maker)[0] == 503
        assert frozen.request("POST", path + "/execute", {}, token=maker)[0] == 503
        assert money(story["database"], story["golden"])[:3] == (80, 62, 18)
    finally:
        frozen.close()
