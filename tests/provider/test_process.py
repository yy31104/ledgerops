"""Independent HTTP/process evidence for the synthetic provider's persistent contract.

No provider posting, canonicalization, signing or ledger helpers compute expectations.
All subprocesses and files belong to tmp_path; a failed readiness check is a failure.
"""

import hashlib
import hmac
import http.client
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = str(UUID(int=100))
WORKSPACE = str(UUID(int=101))
SECRET = bytes(range(32))
TOKEN = "provider-process-tests-command-token-12345"
CONTROL_TOKEN = "provider-process-tests-control-token-67890"


def command(number=1):
    return {
        "workspace_id": WORKSPACE,
        "operation_id": str(UUID(int=number)),
        "merchant_id": str(UUID(int=102)),
        "beneficiary_id": str(UUID(int=103)),
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": "2099-01-01T00:00:00Z",
    }


def key_for(body):
    return f"v1.{NAMESPACE}.{body['workspace_id']}.{body['operation_id']}"


def execution_path(body):
    return f"/v1/executions/{key_for(body)}"


def decode_signed(envelope):
    """Check the public HMAC specification without importing provider code."""
    assert set(envelope) == {"key_id", "payload", "signature"}
    assert envelope["key_id"] == "test-v1"
    payload = envelope["payload"].encode("utf-8")
    signed = b"ledgerops.provider-event:v1\n" + b"test-v1\n" + payload
    expected = hmac.new(SECRET, signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, envelope["signature"])
    decoded = json.loads(payload)
    assert json.dumps(decoded, sort_keys=True, separators=(",", ":")) == envelope["payload"]
    assert decoded["namespace"] == NAMESPACE
    return decoded


def decode_event(envelope):
    decoded = decode_signed(envelope)
    assert decoded["issuer"] == "ledgerops-provider-sim"
    return decoded


def check_page_proof(page, after):
    proof = decode_signed(page["proof"])
    assert proof == {
        "kind": "history_page",
        "namespace": NAMESPACE,
        "watermark": page["watermark"],
        "after": after,
        "next_after": page["next_after"],
        "complete": page["complete"],
        "event_payload_hashes": [
            hashlib.sha256(item["payload"].encode()).hexdigest() for item in page["events"]
        ],
    }


class ProcessProvider:
    def __init__(self, directory, *, controls=True, namespace=NAMESPACE):
        self.directory = directory
        self.path = directory / "provider.sqlite3"
        self.stdout_path = directory / "provider.stdout"
        self.stderr_path = directory / "provider.stderr"
        self.controls = controls
        self.namespace = namespace
        self.process = None
        self.ready = None
        self._err = None

    def start(self):
        assert self.process is None or self.process.poll() is not None
        self._err = self.stderr_path.open("ab")
        args = [
            sys.executable,
            "-u",
            "-m",
            "provider_sim",
            "--database",
            str(self.path),
            "--namespace",
            self.namespace,
            "--key-id",
            "test-v1",
            "--port",
            "0",
        ]
        if self.controls:
            args.append("--test-control")
        self.process = subprocess.Popen(
            args,
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "LEDGEROPS_PROVIDER_HMAC_KEY": SECRET.hex(),
                "LEDGEROPS_PROVIDER_TOKEN": TOKEN,
                "LEDGEROPS_PROVIDER_CONTROL_TOKEN": CONTROL_TOKEN,
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._err,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                assert selector.select(15), "provider did not signal readiness"
                line = self.process.stdout.readline()
            self.stdout_path.write_bytes(line)
            assert line, self.stderr_path.read_text()
            self.ready = json.loads(line)
            assert self.ready["event"] == "ready"
            assert self.ready["pid"] == self.process.pid
            assert self.ready["namespace"] == self.namespace
            assert self.ready["url"].startswith("http://127.0.0.1:")
            self.port = int(self.ready["url"].rsplit(":", 1)[1])
            assert 0 < self.port < 65536
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                    raise AssertionError("provider did not terminate after SIGTERM") from None
            if self.process.stdout is not None:
                self.process.stdout.close()
        if self._err is not None:
            self._err.close()

    def request(self, method, path, body=None, *, token=TOKEN, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        encoded = raw if raw is not None else None if body is None else json.dumps(body).encode()
        actual_headers = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            actual_headers["Authorization"] = f"Bearer {token}"
        try:
            connection.request(method, path, body=encoded, headers=actual_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    def control(self, action, body=None):
        method = "GET" if action == "state" else "POST"
        return self.request(method, f"/__test/{action}", body, token=CONTROL_TOKEN)

    def configure(self, body, **options):
        status, result = self.control("configure", {"provider_key": key_for(body), **options})
        assert status == 200, result


@pytest.fixture
def provider(tmp_path):
    process = ProcessProvider(tmp_path)
    try:
        yield process.start()
    finally:
        process.stop()


def effect_rows(path):
    # Opening this owned test DB also recovers a hot rollback journal after kill.
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT provider_key,principal_minor,fee_minor,gross_minor "
            "FROM effects ORDER BY provider_key"
        ).fetchall()


def wait_for_gate(provider):
    deadline = time.monotonic() + 10
    while True:
        status, state = provider.control("state")
        assert status == 200
        if state["gate"]["reached"]:
            return
        assert time.monotonic() < deadline, "transaction never reached its causal gate"


def test_success_survives_restart_and_replay_has_one_effect(provider):
    body = command()
    status, first = provider.request("PUT", execution_path(body), body)
    assert status == 200
    event = decode_event(first)
    assert event["state"] == "SUCCEEDED"
    assert event["request"] == body
    assert event["provider_key"] == key_for(body)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert event["request_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]
    provider.stop()
    assert provider.process.returncode == 0
    provider.start()
    for method, path, payload in (
        ("GET", execution_path(body), None),
        ("PUT", execution_path(body), body),
        ("POST", execution_path(body) + "/close-if-absent", body),
    ):
        status, replay = provider.request(method, path, payload)
        assert status == 200
        assert replay == first
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]


def test_same_key_changed_payload_conflicts_without_replacing_original(provider):
    body = command()
    assert provider.request("PUT", execution_path(body), body)[0] == 200
    changed = {**body, "principal_minor": "61", "gross_minor": "63"}
    for method, suffix in (("PUT", ""), ("POST", "/close-if-absent")):
        status, _ = provider.request(method, execution_path(body) + suffix, changed)
        assert status == 409
    status, current = provider.request("GET", execution_path(body))
    assert status == 200
    assert decode_event(current)["request"] == body
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]


def test_tombstone_survives_restart_and_blocks_late_put(provider):
    body = command()
    status, closed = provider.request("POST", execution_path(body) + "/close-if-absent", body)
    assert status == 200
    assert decode_event(closed)["state"] == "CLOSED_ABSENT"
    assert effect_rows(provider.path) == []
    provider.stop()
    provider.start()
    status, late = provider.request("PUT", execution_path(body), body)
    assert status == 200
    assert late == closed
    assert effect_rows(provider.path) == []
    changed = {**body, "beneficiary_id": str(UUID(int=104))}
    assert provider.request("PUT", execution_path(body), changed)[0] == 409


def test_pending_execution_is_not_cancelled_by_absence_closure(provider):
    body = command()
    provider.configure(body, put_outcome="ACCEPTED")
    status, accepted = provider.request("PUT", execution_path(body), body)
    assert status == 200
    assert decode_event(accepted)["state"] == "ACCEPTED"
    status, closed = provider.request("POST", execution_path(body) + "/close-if-absent", body)
    assert status == 200
    assert closed == accepted
    assert effect_rows(provider.path) == []
    status, success = provider.control(
        "resolve", {"provider_key": key_for(body), "state": "SUCCEEDED"}
    )
    assert status == 200
    assert decode_event(success)["state"] == "SUCCEEDED"
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]


def test_authoritative_failure_is_durable_and_cannot_later_execute(provider):
    body = command()
    provider.configure(body, put_outcome="FAILED")
    status, failed = provider.request("PUT", execution_path(body), body)
    assert status == 200
    assert decode_event(failed)["state"] == "FAILED"
    provider.stop()
    provider.start()
    status, replay = provider.request("PUT", execution_path(body), body)
    assert status == 200
    assert replay == failed
    assert effect_rows(provider.path) == []


@pytest.mark.parametrize("method", ["PUT", "POST"])
@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_actual_process_death_at_commit_boundary(provider, method, point):
    body = command()
    status, _ = provider.control(
        "arm", {"provider_key": key_for(body), "point": point, "action": "pause"}
    )
    assert status == 200
    path = execution_path(body) + ("/close-if-absent" if method == "POST" else "")
    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(provider.request, method, path, body)
        try:
            wait_for_gate(provider)
            provider.process.kill()
            provider.process.wait(timeout=10)
            assert provider.process.returncode == -signal.SIGKILL
            with pytest.raises((http.client.RemoteDisconnected, ConnectionError, OSError)):
                request.result(timeout=10)
        finally:
            if provider.process.poll() is None:
                provider.control("release", {})
    durable_success = method == "PUT" and point == "after_commit"
    assert effect_rows(provider.path) == (
        [(key_for(body), "60", "2", "62")] if durable_success else []
    )
    provider.stop()
    provider.start()
    status, observation = provider.request("GET", execution_path(body))
    assert status == 200
    observed = decode_event(observation)
    expected = (
        "NOT_FOUND"
        if point == "before_commit"
        else ("SUCCEEDED" if method == "PUT" else "CLOSED_ABSENT")
    )
    assert observed["state"] == expected
    # Resume with the original bytes: a committed tombstone still prevents payment.
    status, replay = provider.request("PUT", execution_path(body), body)
    assert status == 200
    expected_after_replay = (
        "CLOSED_ABSENT" if method == "POST" and point == "after_commit" else "SUCCEEDED"
    )
    assert decode_event(replay)["state"] == expected_after_replay
    assert len(effect_rows(provider.path)) == (0 if expected_after_replay == "CLOSED_ABSENT" else 1)


def test_simultaneous_same_key_requests_share_one_persistent_effect(provider):
    body = command()
    with ThreadPoolExecutor(max_workers=8) as executor:
        replies = list(
            executor.map(lambda _: provider.request("PUT", execution_path(body), body), range(8))
        )
    assert all(status == 200 for status, _ in replies)
    assert all(envelope == replies[0][1] for _, envelope in replies)
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]


@pytest.mark.parametrize("first_method", ["PUT", "POST"])
def test_put_close_serialization_waits_for_actual_first_transaction(provider, first_method):
    body = command()
    status, _ = provider.control(
        "arm", {"provider_key": key_for(body), "point": "before_commit", "action": "pause"}
    )
    assert status == 200
    first_path = execution_path(body) + ("/close-if-absent" if first_method == "POST" else "")
    second_method = "POST" if first_method == "PUT" else "PUT"
    second_path = execution_path(body) + ("/close-if-absent" if second_method == "POST" else "")
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(provider.request, first_method, first_path, body)
        try:
            wait_for_gate(provider)
            assert not first.done()
            # The gate is inside the first SQLite transaction. A competing writer
            # reaches the finite database busy bound; it must not commit around it.
            status, _ = provider.request(second_method, second_path, body)
            assert status == 503
        finally:
            provider.control("release", {})
        first_status, first_event = first.result(timeout=10)
    assert first_status == 200
    second_status, second_event = provider.request(second_method, second_path, body)
    assert second_status == 200
    assert second_event == first_event
    expected = "SUCCEEDED" if first_method == "PUT" else "CLOSED_ABSENT"
    assert decode_event(second_event)["state"] == expected
    assert len(effect_rows(provider.path)) == (1 if first_method == "PUT" else 0)


def test_history_pagination_keeps_fixed_watermark_and_signed_original_events(provider):
    emitted = []
    for index in range(1, 4):
        body = command(index)
        status, envelope = provider.request("PUT", execution_path(body), body)
        assert status == 200
        emitted.append(envelope)
    status, first = provider.request("GET", "/v1/history?after=0&limit=1")
    assert status == 200
    assert first["namespace"] == NAMESPACE
    check_page_proof(first, 0)
    watermark = first["watermark"]
    assert first["events"] == emitted[:1]
    assert not first["complete"]
    later = command(4)
    assert provider.request("PUT", execution_path(later), later)[0] == 200
    found = list(first["events"])
    page = first
    while not page["complete"]:
        after = page["next_after"]
        status, page = provider.request(
            "GET", f"/v1/history?after={after}&watermark={watermark}&limit=1"
        )
        assert status == 200
        assert page["watermark"] == watermark
        check_page_proof(page, after)
        found.extend(page["events"])
        assert len(found) <= 3, "reader crossed its pinned watermark"
    assert found == emitted
    assert [decode_event(item)["source_sequence"] for item in found] == [1, 2, 3]
    assert len(effect_rows(provider.path)) == 4


def test_explicit_signed_contradiction_does_not_rewrite_actual_provider_history(provider):
    body = command()
    status, success = provider.request("PUT", execution_path(body), body)
    assert status == 200
    status, contradiction = provider.control(
        "observation", {"provider_key": key_for(body), "state": "FAILED"}
    )
    assert status == 200
    assert decode_event(contradiction)["state"] == "FAILED"
    assert decode_event(contradiction)["event_id"] != decode_event(success)["event_id"]
    assert provider.request("GET", execution_path(body)) == (200, success)
    status, history = provider.request("GET", "/v1/history")
    assert status == 200
    assert history["events"] == [success]
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]


def test_lagging_not_found_does_not_erase_actual_success(provider):
    body = command()
    status, success = provider.request("PUT", execution_path(body), body)
    assert status == 200
    provider.configure(body, lookup_lag=True)
    status, lag = provider.request("GET", execution_path(body))
    assert status == 200
    assert decode_event(lag)["state"] == "NOT_FOUND"
    assert effect_rows(provider.path) == [(key_for(body), "60", "2", "62")]
    status, actual = provider.request("POST", execution_path(body) + "/close-if-absent", body)
    assert status == 200
    assert actual == success


def test_public_deadline_is_not_permission_to_execute_again(provider):
    # The ledger decides when to close; the provider honors a stable key even for a late PUT.
    body = {**command(), "dispatch_deadline": "2020-01-01T00:00:00Z"}
    status, first = provider.request("PUT", execution_path(body), body)
    assert status == 200
    assert decode_event(first)["state"] == "SUCCEEDED"
    assert provider.request("PUT", execution_path(body), body) == (200, first)
    assert len(effect_rows(provider.path)) == 1


@pytest.mark.parametrize("token", [None, "wrong", CONTROL_TOKEN])
def test_unauthorized_requests_do_not_create_provider_effects(provider, token):
    body = command()
    status, _ = provider.request("PUT", execution_path(body), body, token=token)
    assert status in (401, 403)
    assert effect_rows(provider.path) == []


def test_control_plane_is_separate_and_input_is_bounded(provider):
    assert provider.request("GET", "/__test/state")[0] in (401, 403)
    body = command()
    raw = json.dumps(body)[:-1] + ',"gross_minor":"62"}'
    assert provider.request("PUT", execution_path(body), raw=raw.encode())[0] == 400
    connection = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=5)
    try:
        connection.putrequest("PUT", execution_path(body))
        connection.putheader("Authorization", f"Bearer {TOKEN}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "1000000")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        response.read()
    finally:
        connection.close()
    assert effect_rows(provider.path) == []


def test_test_control_is_not_available_without_explicit_switch(tmp_path):
    provider = ProcessProvider(tmp_path, controls=False)
    try:
        provider.start()
        assert provider.control("state")[0] == 404
        assert provider.control("configure", {"provider_key": key_for(command())})[0] == 404
    finally:
        provider.stop()


def test_service_shutdown_and_startup_logs_do_not_expose_credentials(provider):
    port = provider.port
    provider.stop()
    assert provider.process.returncode == 0
    logs = provider.stdout_path.read_text() + provider.stderr_path.read_text()
    for value in (TOKEN, CONTROL_TOKEN, SECRET.hex()):
        assert value not in logs
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        with pytest.raises(OSError):
            connection.connect()
    finally:
        connection.close()


def test_readiness_reports_effective_sqlite_configuration(provider):
    settings = provider.ready["diagnostics"]
    assert settings["journal_mode"] == "delete"
    assert settings["synchronous"] == 2
    assert settings["foreign_keys"] == 1
    assert 0 < settings["busy_timeout_ms"] < 60_000
    assert settings["integrity_check"] == "ok"
    assert settings["executions"] == settings["events"] == settings["effects"] == 0
    assert settings["sqlite_version"]
