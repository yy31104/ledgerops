"""Actual durable worker claims and independent provider process transport."""

import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.pq import TransactionStatus

from ledgerops.db.payout_dispatch import claim_payout, dispatch_once, due_payouts, finish_claim
from ledgerops.provider_client import ProviderHTTPClient, TransportResponse
from ledgerops.provider_protocol import ProviderTrust
from tests.integration.payout_helpers import (
    NAMESPACE,
    NOW,
    admit,
    fund_merchant,
    money,
    payout_input,
    payout_state,
)
from tests.provider.test_process import SECRET, TOKEN, ProcessProvider, effect_rows

pytestmark = pytest.mark.integration


def trust(command):
    return ProviderTrust(command.provider_namespace, {"test-v1": SECRET})


def signed_lookup(key, *, corrupt=False):
    payload = json.dumps(
        {
            "kind": "lookup",
            "issuer": "ledgerops-provider-sim",
            "contract_version": 1,
            "namespace": NAMESPACE,
            "provider_key": key,
            "observation_id": str(uuid4()),
            "state": "NOT_FOUND",
            "visibility_watermark": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(
        SECRET, b"ledgerops.provider-event:v1\ntest-v1\n" + payload.encode(), hashlib.sha256
    ).hexdigest()
    envelope = {
        "key_id": "test-v1",
        "payload": payload,
        "signature": "0" * 64 if corrupt else signature,
    }
    return TransportResponse(200, json.dumps(envelope).encode())


class Client:
    def __init__(self, conn, callback):
        self.conn, self.callback, self.calls = conn, callback, []

    def request(self, action, key, raw=None):
        assert self.conn.info.transaction_status == TransactionStatus.IDLE
        self.calls.append((action, key, raw))
        return self.callback(action, key, raw)

    def put(self, key, raw):
        return self.request("PUT", key, raw)

    def lookup(self, key):
        return self.request("LOOKUP", key)

    def close(self, key, raw):
        return self.request("CLOSE", key, raw)


def setup_payout(db, golden):
    fund_merchant(db, golden)
    args = payout_input(golden)
    assert admit(db, golden, args).http_status == 202
    return args[1]


def next_due(db):
    with db.connect("reader") as conn:
        return conn.execute("SELECT next_due_at FROM ledger.dispatch_intent").fetchone()[0]


def run(conn, command, client, now):
    return dispatch_once(
        conn,
        command.workspace_id,
        command.operation_id,
        client,
        trust=trust(command),
        clock=lambda: now,
    )


def test_network_begins_only_after_claim_is_committed_and_no_financial_transaction_is_open(
    seeded_db, golden
):
    command = setup_payout(seeded_db, golden)

    def inspect(action, key, raw):
        with seeded_db.connect("reader") as observer:
            assert observer.execute("SELECT knowledge FROM ledger.payout").fetchone() == (
                "UNKNOWN",
            )
            assert observer.execute("SELECT outcome FROM ledger.transport_attempt").fetchone() == (
                "MAY_HAVE_SENT",
            )
            assert observer.execute("SELECT state FROM ledger.dispatch_intent").fetchone() == (
                "IN_FLIGHT",
            )
        assert action == "PUT"
        return TransportResponse(503, b'{"temporary":"unavailable"}')

    with seeded_db.connect("worker") as conn:
        client = Client(conn, inspect)
        result = run(conn, command, client, NOW)
        assert result.outcome == "RESPONSE" and result.http_status == 503
        assert len(client.calls) == 1
    assert money(seeded_db, golden) == (80, 62, 18, 2)
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT outcome,http_status,response_payload FROM ledger.transport_attempt"
        ).fetchone() == ("RESPONSE", 503, b'{"temporary":"unavailable"}')


def test_failed_claim_commit_never_calls_network_and_leaves_no_transport_attempt(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute("""CREATE FUNCTION ledger.test_claim_fail() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='claim failed'; END $$""")
        conn.execute(
            "CREATE CONSTRAINT TRIGGER test_claim_fail AFTER INSERT ON ledger.transport_attempt "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION ledger.test_claim_fail()"
        )
    before = payout_state(seeded_db)
    with seeded_db.connect("worker") as conn:
        client = Client(conn, lambda *args: pytest.fail("network escaped failed claim"))
        with pytest.raises(errors.CheckViolation, match="claim failed"):
            run(conn, command, client, NOW)
        assert client.calls == []
    assert payout_state(seeded_db) == before


def test_abandoned_claim_is_due_after_lease_and_recovers_by_lookup_not_new_put(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("worker") as conn:
        first = claim_payout(conn, command.workspace_id, command.operation_id, clock=lambda: NOW)
        assert first.action == "PUT"
        assert (
            claim_payout(conn, command.workspace_id, command.operation_id, clock=lambda: NOW)
            is None
        )
        assert due_payouts(conn, clock=lambda: NOW) == []
        later = NOW + timedelta(seconds=31)
        assert due_payouts(conn, clock=lambda: later) == [
            (command.workspace_id.value, command.operation_id.value)
        ]
        recovered = claim_payout(
            conn, command.workspace_id, command.operation_id, clock=lambda: later
        )
        assert recovered.action == "LOOKUP" and recovered.attempt_id != first.attempt_id
        assert (recovered.provider_key, recovered.request_bytes) == (
            first.provider_key,
            first.request_bytes,
        )
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_late_old_attempt_completion_cannot_replace_a_newer_claim(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("worker") as conn:
        first = claim_payout(conn, command.workspace_id, command.operation_id, clock=lambda: NOW)
        second = claim_payout(
            conn,
            command.workspace_id,
            command.operation_id,
            clock=lambda: NOW + timedelta(seconds=31),
        )
        with seeded_db.connect("reader") as reader:
            before = reader.execute("SELECT * FROM ledger.dispatch_intent").fetchone()
        assert finish_claim(
            conn,
            first,
            outcome="TIMEOUT",
            next_action="PUT",
            clock=lambda: NOW + timedelta(seconds=32),
        )
        with seeded_db.connect("reader") as reader:
            assert reader.execute("SELECT * FROM ledger.dispatch_intent").fetchone() == before
            assert reader.execute(
                "SELECT outcome FROM ledger.transport_attempt WHERE attempt_id=%s",
                (second.attempt_id,),
            ).fetchone() == ("MAY_HAVE_SENT",)
            assert reader.execute(
                "SELECT outcome FROM ledger.transport_attempt WHERE attempt_id=%s",
                (first.attempt_id,),
            ).fetchone() == ("TIMEOUT",)
        assert not finish_claim(
            conn, first, outcome="TIMEOUT", clock=lambda: NOW + timedelta(seconds=33)
        )


def test_authenticated_not_found_resends_exact_original_identity_before_deadline(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("worker") as conn:

        def respond(action, key, raw):
            if action == "LOOKUP":
                return signed_lookup(key)
            raise TimeoutError("synthetic lost response")

        client = Client(conn, respond)
        assert run(conn, command, client, NOW).outcome == "TIMEOUT"
        observed = run(conn, command, client, next_due(seeded_db))
        assert observed.observation.state == "NOT_FOUND"
        assert run(conn, command, client, next_due(seeded_db)).outcome == "TIMEOUT"
        assert [call[0] for call in client.calls] == ["PUT", "LOOKUP", "PUT"]
        assert client.calls[0][1:] == client.calls[2][1:]
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_invalid_signature_cannot_authorize_absence_resend(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("worker") as conn:
        first = claim_payout(conn, command.workspace_id, command.operation_id, clock=lambda: NOW)
        finish_claim(conn, first, outcome="TIMEOUT", clock=lambda: NOW)
        client = Client(conn, lambda action, key, raw: signed_lookup(key, corrupt=True))
        result = run(conn, command, client, next_due(seeded_db))
        assert result.outcome == "INVALID_RESPONSE" and result.observation is None
        with seeded_db.connect("reader") as reader:
            assert reader.execute("SELECT next_action FROM ledger.dispatch_intent").fetchone() == (
                "LOOKUP",
            )
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_crossing_cutoff_between_claim_and_send_does_not_send_a_new_put(seeded_db, golden):
    command = setup_payout(seeded_db, golden)
    times = iter([NOW, NOW + timedelta(seconds=60), NOW + timedelta(seconds=60)])
    with seeded_db.connect("worker") as conn:
        client = Client(conn, lambda *args: pytest.fail("PUT sent after cutoff"))
        result = dispatch_once(
            conn,
            command.workspace_id,
            command.operation_id,
            client,
            trust=trust(command),
            clock=lambda: next(times),
        )
        assert result.outcome == "TRANSPORT_ERROR" and client.calls == []
        with seeded_db.connect("reader") as reader:
            assert reader.execute(
                "SELECT state,next_action FROM ledger.dispatch_intent"
            ).fetchone() == ("OBSERVING", "CLOSE")
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError])
def test_transport_failures_keep_hold_and_store_transport_fact(seeded_db, golden, failure):
    command = setup_payout(seeded_db, golden)
    with seeded_db.connect("worker") as conn:

        def fail(*args):
            raise failure("synthetic transport fault")

        result = run(conn, command, Client(conn, fail), NOW)
        assert result.outcome == ("TIMEOUT" if failure is TimeoutError else "CONNECTION_ERROR")
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


@pytest.mark.parametrize("provider_has_success", [False, True])
def test_real_provider_cutoff_closure_resolves_tombstone_or_existing_success(
    seeded_db, golden, tmp_path, provider_has_success
):
    command = setup_payout(seeded_db, golden)
    provider = ProcessProvider(tmp_path, namespace=NAMESPACE)
    try:
        provider.start()
        with seeded_db.connect("worker") as conn:
            client = ProviderHTTPClient(provider.ready["url"], TOKEN)
            if provider_has_success:
                # The provider is ahead of local processing; no local resolver
                # has consumed the hold before the cutoff recovery call.
                with seeded_db.connect("reader") as reader:
                    key, raw = reader.execute(
                        "SELECT provider_key,provider_request FROM ledger.payout"
                    ).fetchone()
                assert client.put(key, raw).http_status == 200
                assert money(seeded_db, golden)[:3] == (80, 62, 18)
            result = run(conn, command, client, NOW + timedelta(seconds=61))
            assert result.claim.action == "CLOSE"
            expected = "SUCCEEDED" if provider_has_success else "CLOSED_ABSENT"
            assert result.observation.state == expected
            assert len(effect_rows(provider.path)) == int(provider_has_success)
            assert money(seeded_db, golden)[:3] == (
                (18, 0, 18) if provider_has_success else (80, 0, 80)
            )
            late = client.put(result.claim.provider_key, result.claim.request_bytes)
            assert json.loads(json.loads(late.body)["payload"])["state"] == expected
            assert len(effect_rows(provider.path)) == int(provider_has_success)
    finally:
        provider.stop()


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_unknown_claim_commit_never_sends_and_recovery_observes_saved_claim(
    seeded_db, golden, fault
):
    import psycopg

    from tests.integration.postgres_fault_proxy import PostgresCommitProxy
    from tests.integration.test_purchase_recovery import (
        _runtime_connection_settings,
        _wait_backend_gone,
    )

    command = setup_payout(seeded_db, golden)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        settings = _runtime_connection_settings(seeded_db, proxy.port)
        settings.update(user="ledger_worker", password=seeded_db.worker_password)
        with psycopg.connect(**settings) as conn:
            pid = conn.info.backend_pid
            client = Client(conn, lambda *args: pytest.fail("network after unknown claim commit"))
            with pytest.raises(psycopg.OperationalError):
                run(conn, command, client, NOW)
            assert client.calls == []
        assert proxy.finished.wait(5) and proxy.frontend_commit_seen.is_set()
        assert proxy.fault_applied.is_set()
        assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    _wait_backend_gone(seeded_db, pid)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (
            int(fault == "after-commit"),
        )
    with seeded_db.connect("worker") as conn:
        recovered = claim_payout(
            conn,
            command.workspace_id,
            command.operation_id,
            clock=lambda: NOW + timedelta(seconds=31),
        )
        assert recovered.action == ("LOOKUP" if fault == "after-commit" else "PUT")
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_mismatched_retained_provider_instruction_is_refused_before_claim_or_network(
    seeded_db, golden
):
    from ledgerops.db.admission import InvariantViolation

    command = setup_payout(seeded_db, golden)
    # Explicit privileged defect injection models historical runtime code writing
    # a self-consistent digest for the wrong external amount. The immutable
    # operation payload and HELD62 remain the independent business authority.
    with seeded_db.connect("owner") as conn, conn.transaction():
        raw = conn.execute("SELECT provider_request FROM ledger.payout").fetchone()[0]
        request = json.loads(raw)
        request.update(principal_minor="598", gross_minor="600")
        wrong = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        conn.execute("ALTER TABLE ledger.payout DISABLE TRIGGER payout_transition")
        conn.execute(
            "UPDATE ledger.payout SET provider_request=%s,provider_request_sha256=sha256(%s)",
            (wrong, wrong),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.payout ENABLE TRIGGER payout_transition")
    before = payout_state(seeded_db)
    with seeded_db.connect("worker") as conn:
        client = Client(
            conn, lambda *args: pytest.fail("invalid retained instruction reached provider")
        )
        with pytest.raises(InvariantViolation):
            run(conn, command, client, NOW)
        assert client.calls == []
    assert payout_state(seeded_db) == before
    assert before["transport_attempt"] == []
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
