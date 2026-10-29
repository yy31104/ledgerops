"""HTTP boundary correctness against real Gunicorn and PostgreSQL roles."""

import json

import pytest

from tests.integration.http_helpers import ProcessService
from tests.integration.purchase_helpers import purchase_input, retained_rows
from tests.integration.test_authentication import provision

pytestmark = pytest.mark.integration


@pytest.fixture
def service(seeded_db, tmp_path):
    process = ProcessService(seeded_db, tmp_path / "service")
    try:
        yield process
    finally:
        process.close()


def payload(golden):
    row = golden["purchase_request"]
    return {
        name: str(row[name])
        for name in (
            "operation_id",
            "customer_account_id",
            "merchant_account_id",
            "amount_minor",
            "currency",
        )
    }


def test_actual_server_authenticated_purchase_replay_and_conflict(service, seeded_db, golden):
    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    assert service.request("GET", "/health/ready")[0] == 200
    assert service.request("GET", "/v1/accounts")[0] == 401
    status, raw, _ = service.request("GET", "/v1/accounts", token=issued.token)
    assert status == 200 and json.loads(raw)["accounts"][0]["booked"] == "100"
    request = payload(golden)
    status, raw, headers = service.request(
        "POST",
        "/v1/purchases",
        request,
        token=issued.token,
        headers={"Idempotency-Key": "real-http-purchase"},
    )
    assert status == 201 and headers["Cache-Control"] == "no-store"
    saved = raw
    assert service.request(
        "POST",
        "/v1/purchases",
        request,
        token=issued.token,
        headers={"Idempotency-Key": "real-http-purchase"},
    )[:2] == (201, saved)
    before = retained_rows(seeded_db)
    assert (
        service.request(
            "POST",
            "/v1/purchases",
            {**request, "amount_minor": "79"},
            token=issued.token,
            headers={"Idempotency-Key": "real-http-purchase"},
        )[0]
        == 409
    )
    assert retained_rows(seeded_db) == before
    assert issued.token not in service.logs()
    events = [json.loads(line) for line in service.stdout.read_text().splitlines()]
    assert any(
        event.get("route") == "purchase" and event.get("operation_id") == request["operation_id"]
        for event in events
    )


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("duplicate_json", 400),
        ("unknown_field", 400),
        ("bad_money", 400),
        ("float_money", 400),
        ("missing_key", 400),
        ("huge_body", 413),
        ("wrong_type", 415),
        ("wrong_host", 400),
        ("wrong_origin", 403),
        ("no_token", 401),
        ("fake_token", 401),
        ("mutation_query", 400),
    ],
)
def test_real_http_rejects_invalid_requests_without_business_mutation(
    service, seeded_db, golden, fault, expected
):
    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    before = retained_rows(seeded_db)
    request = payload(golden)
    route = "/v1/purchases"
    headers = {"Idempotency-Key": "bad-input", "Content-Type": "application/json"}
    token = issued.token
    if fault == "duplicate_json":
        request = b'{"operation_id":"a","operation_id":"b"}'
    elif fault == "unknown_field":
        request["actor_id"] = str(principal.actor_id)
    elif fault == "bad_money":
        request["amount_minor"] = "-1"
    elif fault == "float_money":
        request["amount_minor"] = 80.0
    elif fault == "missing_key":
        headers.pop("Idempotency-Key")
    elif fault == "huge_body":
        request = b" " * 16385
    elif fault == "wrong_type":
        headers["Content-Type"] = "text/plain"
    elif fault == "wrong_host":
        headers["Host"] = "evil.example"
    elif fault == "wrong_origin":
        headers["Origin"] = "https://evil.example"
    elif fault == "no_token":
        token = None
    elif fault == "fake_token":
        token = "lo1_" + "x" * 43
    else:
        route += "?role=admin"
    status, raw, _ = service.request("POST", route, request, token=token, headers=headers)
    assert status == expected, raw
    assert retained_rows(seeded_db) == before
    assert issued.token not in service.logs()


def test_restore_config_keeps_database_normal_unwritable(seeded_db, golden, tmp_path):
    process = ProcessService(seeded_db, tmp_path / "restore-service", LEDGEROPS_RESTORE_MODE="true")
    try:
        principal, _, _ = purchase_input(golden)
        issued = provision(seeded_db, principal)
        before = retained_rows(seeded_db)
        assert process.request("GET", "/health/live")[0] == 200
        assert process.request("GET", "/health/ready")[0] == 503
        assert (
            process.request(
                "POST",
                "/v1/purchases",
                payload(golden),
                token=issued.token,
                headers={"Idempotency-Key": "disabled"},
            )[0]
            == 503
        )
        assert retained_rows(seeded_db) == before
    finally:
        process.close()


def test_revocation_between_http_requests_blocks_even_replay(service, seeded_db, golden):
    from ledgerops.authentication import revoke_credential
    from tests.integration.test_authentication import ADMIN

    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    args = ("POST", "/v1/purchases", payload(golden))
    assert (
        service.request(*args, token=issued.token, headers={"Idempotency-Key": "revoked"})[0] == 201
    )
    with seeded_db.connect("auth_admin") as conn:
        revoke_credential(
            conn,
            str(principal.workspace_id),
            str(issued.credential_id),
            admin_actor_id=ADMIN,
            reason="HTTP revocation",
        )
    before = retained_rows(seeded_db)
    assert (
        service.request(*args, token=issued.token, headers={"Idempotency-Key": "revoked"})[0] == 401
    )
    assert retained_rows(seeded_db) == before


def test_schema_damage_is_not_ready_but_process_stays_live(service, seeded_db):
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.schema_migration SET sha256='damaged' "
            "WHERE version='009_authentication.sql'"
        )
    assert service.request("GET", "/health/live")[0] == 200
    assert service.request("GET", "/health/ready")[0] == 503


def test_payout_quote_hold_and_authenticated_status_share_real_contract(
    seeded_db, golden, tmp_path
):
    from tests.integration.payout_helpers import (
        BENEFICIARY,
        NAMESPACE,
        fund_merchant,
        money,
        payout_input,
    )

    principal, command, _ = payout_input(golden)
    fund_merchant(seeded_db, golden)
    issued = provision(
        seeded_db,
        principal,
        scopes=["payout:write", "payout:read", "evidence:read", "accounts:read"],
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": [
                    {
                        "workspace_id": str(principal.workspace_id),
                        "provider_namespace": NAMESPACE,
                        "fixed_fee_minor": 2,
                        "dispatch_ttl_seconds": 60,
                        "beneficiary_owners": {BENEFICIARY: str(principal.actor_id)},
                    }
                ],
            }
        )
    )
    policy.chmod(0o600)
    process = ProcessService(
        seeded_db, tmp_path / "payout-service", LEDGEROPS_PAYOUT_POLICY_FILE=str(policy)
    )
    try:
        request = {
            "operation_id": str(command.operation_id),
            "merchant_account_id": str(command.merchant_account_id),
            "beneficiary_id": BENEFICIARY,
            "provider_namespace": NAMESPACE,
            "principal_minor": "60",
            "fee_minor": "2",
            "currency": "PLN",
            "contract_version": 1,
        }
        status, raw, _ = process.request(
            "POST",
            "/v1/payouts",
            request,
            token=issued.token,
            headers={"Idempotency-Key": "http-payout"},
        )
        assert status == 202, raw
        assert money(seeded_db, golden)[:3] == (80, 62, 18)
        status, raw, _ = process.request(
            "GET", "/v1/payouts/" + str(command.operation_id), token=issued.token
        )
        state = json.loads(raw)
        assert (
            status == 200
            and state["knowledge"] == "NOT_SENT"
            and state["reservation_state"] == "HELD"
        )
        status, raw, _ = process.request(
            "GET", "/v1/investigation/operations/" + str(command.operation_id), token=issued.token
        )
        case = json.loads(raw)
        assert status == 200 and case["operation"]["http_status"] == 202 and case["entries"] == []
        assert case["payout"]["gross_minor"] == "62" and b"provider_request_hex" not in raw
        status, raw, _ = process.request("GET", "/v1/metrics", token=issued.token)
        assert status == 200 and json.loads(raw)["unresolved_count"] == 1
        assert issued.token not in process.logs()
    finally:
        process.close()


def test_console_is_bundled_scoped_static_and_uses_memory_only_credentials(service):
    status, raw, headers = service.request("GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "账本调查台".encode() in raw
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    status, script, _ = service.request("GET", "/assets/console.js")
    assert status == 200
    for forbidden in (b"innerHTML", b"localStorage", b"sessionStorage", b"eval("):
        assert forbidden not in script
    assert b"sessionGeneration" in script and b"pagehide" in script
    assert service.request("GET", "/assets/console.css")[0] == 200
    assert service.request("GET", "/assets/../../authentication.py")[0] == 404


@pytest.mark.parametrize("sequence", [1, 2**53 + 1])
def test_authenticated_callback_settles_once_and_rejects_bad_signature_before_database(
    seeded_db, golden, tmp_path, sequence
):
    from copy import deepcopy

    from psycopg.conninfo import make_conninfo

    from tests.integration.payout_helpers import money
    from tests.integration.payout_observation_helpers import event, prepared_payout
    from tests.provider.test_process import SECRET

    command = prepared_payout(seeded_db, golden)
    keys = tmp_path / "provider-keys.json"
    keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
    keys.chmod(0o600)
    dsn = make_conninfo(
        host="127.0.0.1",
        port=seeded_db.port,
        dbname=seeded_db.name,
        user="ledger_worker",
        password=seeded_db.worker_password,
    )
    process = ProcessService(
        seeded_db,
        tmp_path / "callback-service",
        LEDGEROPS_CALLBACK_DSN=dsn,
        LEDGEROPS_CALLBACK_NAMESPACE=str(command.provider_namespace),
        LEDGEROPS_CALLBACK_KEYS_FILE=str(keys),
    )
    try:
        route = f"/v1/provider/callbacks/{command.workspace_id}/{command.operation_id}"
        envelope = json.loads(event(seeded_db, command, sequence=sequence))
        bad = deepcopy(envelope)
        bad["signature"] = "0" * 64
        before = retained_rows(seeded_db)
        assert process.request("POST", route, bad)[0] == 401
        assert retained_rows(seeded_db) == before
        with seeded_db.connect("reader") as conn:
            assert conn.execute(
                "SELECT count(*) FROM ledger.observation_quarantine"
            ).fetchone() == (0,)
        status, raw, _ = process.request("POST", route, envelope)
        assert status == 202 and json.loads(raw)["status"] == "APPLIED", raw
        assert money(seeded_db, golden)[:3] == (18, 0, 18)
        status, raw, _ = process.request("POST", route, envelope)
        assert status == 202 and json.loads(raw)["status"] == "REPLAYED"
        assert money(seeded_db, golden)[:3] == (18, 0, 18)
        from tests.integration.payout_helpers import payout_input

        principal = payout_input(golden)[0]
        issued = provision(seeded_db, principal, scopes=["payout:read", "evidence:read"])
        status, raw, _ = process.request(
            "GET", "/v1/payouts/" + str(command.operation_id), token=issued.token
        )
        state = json.loads(raw)
        assert status == 200 and state["source_sequence"] == str(sequence)
        assert state["recent_observations"][0]["source_sequence"] == str(sequence)
        status, raw, _ = process.request(
            "GET", "/v1/investigation/operations/" + str(command.operation_id), token=issued.token
        )
        assert status == 200 and json.loads(raw)["recent_observations"][0][
            "source_sequence"
        ] == str(sequence)
        for secret in (SECRET.hex(), dsn, seeded_db.worker_password):
            assert secret not in process.logs()
    finally:
        process.close()


def test_operator_cannot_investigate_another_workspace_even_when_actor_uuid_matches(
    service, seeded_db, golden
):
    from copy import deepcopy
    from uuid import uuid4

    from tests.integration.helpers import post_fixture, seed_accounts
    from tests.integration.purchase_helpers import invoke

    other = deepcopy(golden)
    old = golden["workspace"]["id"]
    new = str(uuid4())
    other = json.loads(json.dumps(other).replace(old, new))
    other["purchase_request"]["operation_id"] = str(uuid4())
    seed_accounts(seeded_db, other)
    post_fixture(seeded_db, other, "OPENING")
    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal, scopes=["evidence:read", "accounts:read"])
    assert invoke(seeded_db, *purchase_input(other)).http_status == 201
    target = other["purchase_request"]["operation_id"]
    assert (
        service.request("GET", "/v1/investigation/operations/" + target, token=issued.token)[0]
        == 404
    )


def test_operation_receipt_is_scoped_to_the_authenticated_client(service, seeded_db, golden):
    from dataclasses import replace
    from uuid import uuid4

    from ledgerops.domain import ClientId

    principal, command, _ = purchase_input(golden)
    issued = provision(seeded_db, principal, scopes=["purchase:write", "operations:read"])
    assert (
        service.request(
            "POST",
            "/v1/purchases",
            payload(golden),
            token=issued.token,
            headers={"Idempotency-Key": "receipt"},
        )[0]
        == 201
    )
    path = "/v1/operations/" + str(command.operation_id)
    status, raw, _ = service.request("GET", path, token=issued.token)
    assert status == 200 and json.loads(raw)["http_status"] == 201
    other = replace(principal, client_id=ClientId.parse(str(uuid4())))
    from ledgerops import authentication as auth
    from tests.integration.test_authentication import ADMIN

    with seeded_db.connect("auth_admin") as conn:
        auth.set_client(
            conn,
            str(other.workspace_id),
            str(other.client_id),
            str(other.actor_id),
            admin_actor_id=ADMIN,
            reason="second client",
        )
        alternative = auth.issue_credential(
            conn,
            str(other.workspace_id),
            str(other.client_id),
            ["operations:read"],
            admin_actor_id=ADMIN,
            reason="second client token",
        )
    assert service.request("GET", path, token=alternative.token)[0] == 404


def test_gunicorn_enforces_header_bounds_without_logging_sensitive_header_values(
    service, seeded_db, golden
):
    import http.client

    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=5)
    try:
        conn.putrequest("GET", "/health/live")
        for i in range(33):
            conn.putheader("X-Noise-" + str(i), issued.token)
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 431
        response.read()
    finally:
        conn.close()
    assert issued.token not in service.logs()


def test_proxy_https_claims_are_ignored_from_untrusted_peer(seeded_db, golden, tmp_path):
    process = ProcessService(
        seeded_db,
        tmp_path / "production-service",
        LEDGEROPS_ENV="production",
        LEDGEROPS_TLS_TERMINATED="true",
        LEDGEROPS_TRUSTED_PROXIES="203.0.113.10",
    )
    try:
        principal, _, _ = purchase_input(golden)
        issued = provision(seeded_db, principal)
        assert process.request("GET", "/health/live")[0] == 200
        assert (
            process.request(
                "GET", "/v1/accounts", token=issued.token, headers={"X-Forwarded-Proto": "https"}
            )[0]
            == 400
        )
        assert process.request("GET", "/")[0] == 400
    finally:
        process.close()


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_http_unknown_commit_recovers_by_original_identity(seeded_db, golden, tmp_path, fault):
    from psycopg.conninfo import make_conninfo

    from tests.integration.postgres_fault_proxy import PostgresCommitProxy

    principal, command, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        dsn = make_conninfo(
            host="127.0.0.1",
            port=proxy.port,
            dbname=seeded_db.name,
            user="ledger_service",
            password=seeded_db.service_password,
            sslmode="disable",
            gssencmode="disable",
        )
        process = ProcessService(seeded_db, tmp_path / "fault-server", LEDGEROPS_SERVICE_DSN=dsn)
        try:
            status, raw, _ = process.request(
                "POST",
                "/v1/purchases",
                payload(golden),
                token=issued.token,
                headers={"Idempotency-Key": "same-http-identity"},
            )
            assert status == 503 and json.loads(raw)["error"]["retryable"]
            assert proxy.fault_applied.wait(3)
            assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
        finally:
            process.close()
    healthy = ProcessService(seeded_db, tmp_path / "healthy-server")
    try:
        status, raw, _ = healthy.request(
            "POST",
            "/v1/purchases",
            payload(golden),
            token=issued.token,
            headers={"Idempotency-Key": "same-http-identity"},
        )
        assert status == 201, raw
        with seeded_db.connect("reader") as conn:
            assert conn.execute(
                "SELECT count(*) FROM ledger.journal WHERE operation_id=%s",
                (command.operation_id.value,),
            ).fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM ledger.request_result").fetchone() == (1,)
    finally:
        healthy.close()


def test_sigterm_drains_observed_inflight_purchase_and_closes_connections(
    service, seeded_db, golden
):
    import signal
    from threading import Event
    from time import monotonic

    from tests.integration.concurrency_helpers import finish_call, start_call

    principal, _, _ = purchase_input(golden)
    issued = provision(seeded_db, principal)
    with seeded_db.connect("owner") as blocker, seeded_db.connect("admin") as observer:
        with blocker.transaction():
            blocker.execute(
                "SELECT account_id FROM ledger.account_balance WHERE account_id=%s FOR UPDATE",
                (golden["purchase_request"]["customer_account_id"],),
            )
            request = start_call(
                lambda: service.request(
                    "POST",
                    "/v1/purchases",
                    payload(golden),
                    token=issued.token,
                    headers={"Idempotency-Key": "drain-inflight"},
                )
            )
            deadline = monotonic() + 3
            while True:
                waits = observer.execute(
                    "SELECT pid,query FROM pg_stat_activity WHERE datname=%s "
                    "AND application_name='ledgerops-api' AND %s=ANY(pg_blocking_pids(pid))",
                    (seeded_db.name, blocker.info.backend_pid),
                ).fetchall()
                if waits:
                    assert "FOR UPDATE" in waits[0][1]
                    break
                assert not request.done.is_set() and monotonic() < deadline
                Event().wait(0.01)
            service.process.send_signal(signal.SIGTERM)
            deadline = monotonic() + 3
            while "Handling signal: term" not in service.stderr.read_text():
                assert monotonic() < deadline
                Event().wait(0.01)
            assert service.process.poll() is None
        kind, result = finish_call(request)
        assert kind == "returned" and result[0] == 201, result
    assert service.process.wait(timeout=12) == 0
