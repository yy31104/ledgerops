"""Actual ledger/provider processes: durable external effects and bounded recovery.

Ordering comes from observed provider gates, durable rows and process exits. Poll
timeouts are watchdogs, never evidence that an operation committed or rolled back.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus

from ledgerops.db.maintenance import resume_workspace
from ledgerops.export import capture_snapshot, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.payout_helpers import (
    NAMESPACE,
    admit,
    fund_merchant,
    money,
    payout_input,
    payout_policy,
    payout_state,
)
from tests.integration.test_maintenance import ACTOR, _apply, _freeze, _preview
from tests.integration.test_payout_resolution import check_final
from tests.provider.test_process import SECRET, TOKEN, ProcessProvider, effect_rows, wait_for_gate

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def eventually(check, message, *, timeout=12):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, message
        Event().wait(0.02)  # observation polling only, not an assumed transaction schedule


class ProcessWorker:
    def __init__(self, db, directory, url, *, overrides=None, args=()):
        directory.mkdir()
        self.db = db
        self.directory = directory
        key_file = directory / "provider-keys.json"
        key_file.write_text(json.dumps({"test-v1": SECRET.hex()}))
        key_file.chmod(0o600)
        self.dsn = make_conninfo(
            host="127.0.0.1",
            port=db.port,
            dbname=db.name,
            user="ledger_worker",
            password=db.worker_password,
        )
        self.environment = {
            **os.environ,
            "LEDGEROPS_WORKER_DSN": self.dsn,
            "LEDGEROPS_PROVIDER_URL": url,
            "LEDGEROPS_PROVIDER_TOKEN": TOKEN,
            "LEDGEROPS_PROVIDER_NAMESPACE": NAMESPACE,
            "LEDGEROPS_PROVIDER_KEYS_FILE": str(key_file),
            "LEDGEROPS_RESTORE_MODE": "false",
            "LEDGEROPS_DISPATCH_ENABLED": "true",
            "LEDGEROPS_WORKER_POLL_MS": "50",
            "LEDGEROPS_WORKER_LEASE_SECONDS": "1",
            **(overrides or {}),
        }
        self.stdout_path = directory / "worker.stdout"
        self.stderr_path = directory / "worker.stderr"
        self._out = self.stdout_path.open("wb")
        self._err = self.stderr_path.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "ledgerops.worker", *args],
            cwd=ROOT,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=self._out,
            stderr=self._err,
        )

    def events(self):
        return [json.loads(line) for line in self.stdout_path.read_text().splitlines() if line]

    def ready(self):
        def check():
            ready = [row for row in self.events() if row["event"] == "worker_ready"]
            if ready:
                assert ready[0]["pid"] == self.process.pid
                return ready[0]
            assert self.process.poll() is None, self.logs()
            return None

        return eventually(check, "ledger worker did not become ready")

    def logs(self):
        return self.stdout_path.read_text() + self.stderr_path.read_text()

    def wait(self, expected=0):
        # A valid ACCEPTED/NOT_FOUND observation schedules another scan 30s
        # later. Observe that real durable schedule instead of rewriting it.
        assert self.process.wait(timeout=45) == expected, self.logs()
        self.assert_connections_closed()
        self.assert_no_secrets()
        return self.events()

    def assert_connections_closed(self):
        pids = [row["backend_pid"] for row in self.events() if row["event"] == "worker_ready"]
        with self.db.connect("admin") as conn:
            eventually(
                lambda: conn.execute(
                    "SELECT count(*)=0 FROM pg_stat_activity WHERE pid=ANY(%s)",
                    (pids,),
                ).fetchone()[0],
                "terminated worker left an owned PostgreSQL connection",
            )

    def assert_no_secrets(self):
        actual_dsn = self.environment["LEDGEROPS_WORKER_DSN"]
        configured_password = conninfo_to_dict(actual_dsn)["password"]
        for secret in (
            self.db.worker_password,
            configured_password,
            TOKEN,
            SECRET.hex(),
            actual_dsn,
        ):
            assert secret not in self.logs()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                    pytest.fail("worker ignored bounded SIGTERM shutdown")
            self.assert_connections_closed()
            self.assert_no_secrets()
        finally:
            self._out.close()
            self._err.close()


@pytest.fixture
def worker_factory(seeded_db, tmp_path):
    workers = []

    def start(url, *args, **overrides):
        worker = ProcessWorker(
            seeded_db, tmp_path / f"worker-{len(workers)}", url, overrides=overrides, args=args
        )
        workers.append(worker)
        return worker

    try:
        yield start
    finally:
        for worker in workers:
            worker.close()


@pytest.fixture
def payment_provider(tmp_path):
    directory = tmp_path / "payment-provider"
    directory.mkdir()
    provider = ProcessProvider(directory, namespace=NAMESPACE)
    try:
        yield provider.start()
    finally:
        provider.stop()


class TrafficTrap:
    """A loopback transport barrier that never forwards to the real provider."""

    def __init__(self):
        self.received = Event()
        self.release = Event()
        self.requests = []
        trap = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                self.handle_request()

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                trap.requests.append((self.command, self.path, body))
                trap.received.set()
                trap.release.wait(10)
                try:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        assert not self.thread.is_alive()


def live_admission(db, golden, *, ttl=60):
    fund_merchant(db, golden)
    args = payout_input(golden)
    receipt = admit(
        db,
        golden,
        args,
        policy=payout_policy(golden, dispatch_ttl_seconds=ttl),
        now=datetime.now(UTC),
    )
    assert receipt.http_status == 202
    with db.connect("reader") as conn:
        key, raw = conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone()
    return args, receipt, key, raw


def assert_durable_claim(db, golden, worker, *, action="PUT"):
    with db.connect("reader") as conn:
        assert conn.execute("SELECT knowledge FROM ledger.payout").fetchone() == ("UNKNOWN",)
        assert conn.execute("SELECT state FROM ledger.dispatch_intent").fetchone() == ("IN_FLIGHT",)
        assert conn.execute("SELECT action,outcome FROM ledger.transport_attempt").fetchall() == [
            (action, "MAY_HAVE_SENT")
        ]
    assert money(db, golden)[:3] == (80, 62, 18)
    with db.connect("admin") as conn:
        assert conn.execute(
            "SELECT state,xact_start FROM pg_stat_activity WHERE pid=%s",
            (
                [row["backend_pid"] for row in worker.events() if row["event"] == "worker_ready"][
                    -1
                ],
            ),
        ).fetchall() == [("idle", None)]


def assert_export_and_effect(db, golden, provider, key, root, *, state):
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    with db.connect("reader") as conn:
        path = export_workspace(
            conn, golden["workspace"]["id"], root, code_revision=revision, dirty=True
        )
    snapshot = load_export(path)
    report = verify_snapshot(snapshot)
    assert report.healthy and report.external_evidence["payouts_resolved"]
    assert report.external_evidence["signature_authenticity"] == "NOT_VERIFIED_NO_TRUST_KEYS"
    assert report.external_evidence["independent_provider_history"] == "NOT_PROVIDED"
    # Separately read independent SQLite storage. The ledger's verifier does not
    # certify provider history; these literal scenario expectations are extra evidence.
    assert effect_rows(provider.path) == ([(key, "60", "2", "62")] if state == "SUCCEEDED" else [])
    assert len(snapshot["payout_resolutions"]) == 1
    assert snapshot["payout_resolutions"][0]["state"] == state
    assert [row["booked"] for row in snapshot["balances"]] == (
        ["38", "20", "18", "0"] if state == "SUCCEEDED" else ["100", "20", "80", "0"]
    )


@pytest.mark.parametrize("lagged_lookup", [False, True])
def test_sigkill_after_provider_commit_recovers_same_key_and_exactly_one_external_effect(
    seeded_db, golden, payment_provider, worker_factory, tmp_path, lagged_lookup
):
    args, original, key, raw = live_admission(seeded_db, golden)
    provider = payment_provider
    assert (
        provider.control("arm", {"provider_key": key, "point": "after_commit", "action": "pause"})[
            0
        ]
        == 200
    )
    first = worker_factory(provider.ready["url"])
    first.ready()
    wait_for_gate(provider)
    assert effect_rows(provider.path) == [(key, "60", "2", "62")]
    assert_durable_claim(seeded_db, golden, first)
    first.process.kill()
    first.wait(-signal.SIGKILL)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    assert provider.control("release", {})[0] == 200
    if lagged_lookup:
        assert provider.control("configure", {"provider_key": key, "lookup_lag": True})[0] == 200
        lagged = worker_factory(provider.ready["url"], "--max-calls", "1")
        lagged.wait()
        assert money(seeded_db, golden)[:3] == (80, 62, 18)
        with seeded_db.connect("reader") as conn:
            assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
        assert provider.control("configure", {"provider_key": key, "lookup_lag": False})[0] == 200
    recovered = worker_factory(provider.ready["url"], "--max-calls", "1")
    recovered.wait()
    check_final(seeded_db, golden, args[1], "SUCCEEDED")
    replay = admit(seeded_db, golden, args, now=datetime.now(UTC))
    assert (replay.http_status, replay.body, replay.replayed) == (202, original.body, True)
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone() == (key, raw)
        actions = conn.execute(
            "SELECT action FROM ledger.transport_attempt ORDER BY started_at"
        ).fetchall()
        assert actions == (
            [("PUT",), ("LOOKUP",), ("PUT",)] if lagged_lookup else [("PUT",), ("LOOKUP",)]
        )
    assert_export_and_effect(
        seeded_db, golden, provider, key, tmp_path / "exports", state="SUCCEEDED"
    )


def test_kill_after_durable_claim_before_provider_receives_then_close_tombstone_prevents_late_put(
    seeded_db, golden, payment_provider, worker_factory, tmp_path
):
    args, _, key, raw = live_admission(seeded_db, golden, ttl=5)
    with TrafficTrap() as trap:
        first = worker_factory(trap.url)
        first.ready()
        assert trap.received.wait(10)
        assert trap.requests == [("PUT", f"/v1/executions/{key}", raw)]
        assert_durable_claim(seeded_db, golden, first)
        assert effect_rows(payment_provider.path) == []
        first.process.kill()
        first.wait(-signal.SIGKILL)
    with seeded_db.connect("reader") as conn:
        eventually(
            lambda: conn.execute(
                "SELECT clock_timestamp()>=dispatch_deadline FROM ledger.payout"
            ).fetchone()[0],
            "real dispatch deadline was not reached",
        )
    recovered = worker_factory(payment_provider.ready["url"], "--max-calls", "1")
    recovered.wait()
    check_final(seeded_db, golden, args[1], "CLOSED_ABSENT")
    status, late = payment_provider.request("PUT", f"/v1/executions/{key}", raw=raw)
    assert status == 200 and json.loads(late["payload"])["state"] == "CLOSED_ABSENT"
    assert_export_and_effect(
        seeded_db, golden, payment_provider, key, tmp_path / "exports", state="CLOSED_ABSENT"
    )


def test_pending_survives_both_process_restarts_and_only_authoritative_success_consumes(
    seeded_db, golden, payment_provider, worker_factory, tmp_path
):
    args, _, key, _ = live_admission(seeded_db, golden)
    provider = payment_provider
    assert provider.control("configure", {"provider_key": key, "put_outcome": "ACCEPTED"})[0] == 200
    first = worker_factory(provider.ready["url"], "--max-calls", "1")
    first.wait()
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
    assert report.healthy and not report.external_evidence["payouts_resolved"]
    assert report.external_evidence["unresolved_payout_ids"] == [str(args[1].operation_id)]
    provider.stop()
    provider.start()
    assert provider.control("resolve", {"provider_key": key, "state": "SUCCEEDED"})[0] == 200
    recovered = worker_factory(provider.ready["url"], "--max-calls", "1")
    recovered.wait()
    check_final(seeded_db, golden, args[1], "SUCCEEDED")
    assert_export_and_effect(
        seeded_db, golden, provider, key, tmp_path / "exports", state="SUCCEEDED"
    )


def test_authoritative_failure_releases_hold_without_creating_a_payment(
    seeded_db, golden, payment_provider, worker_factory, tmp_path
):
    args, _, key, _ = live_admission(seeded_db, golden)
    assert (
        payment_provider.control("configure", {"provider_key": key, "put_outcome": "FAILED"})[0]
        == 200
    )
    worker_factory(payment_provider.ready["url"], "--max-calls", "1").wait()
    check_final(seeded_db, golden, args[1], "FAILED")
    assert_export_and_effect(
        seeded_db, golden, payment_provider, key, tmp_path / "exports", state="FAILED"
    )


def test_restore_physical_flag_blocks_every_network_call_even_when_database_says_normal(
    seeded_db, golden, worker_factory
):
    live_admission(seeded_db, golden)
    before = payout_state(seeded_db)
    with TrafficTrap() as trap:
        worker = worker_factory(trap.url, "--once", LEDGEROPS_RESTORE_MODE="true")
        events = worker.wait(expected=2)
        assert [event["event"] for event in events] == ["configuration_error"]
        assert trap.requests == []
    assert payout_state(seeded_db) == before
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("NORMAL",)


@pytest.mark.parametrize("role", ["admin", "app", "bad_password"])
def test_invalid_database_identity_fails_before_ready_or_network_and_redacts_credentials(
    seeded_db, golden, worker_factory, role
):
    live_admission(seeded_db, golden)
    before = payout_state(seeded_db)
    user, password = {
        "admin": ("postgres", seeded_db.admin_password),
        "app": ("ledger_app", seeded_db.app_password),
        "bad_password": ("ledger_worker", "invalid-worker-credential-sensitive-text"),
    }[role]
    dsn = make_conninfo(
        host="127.0.0.1", port=seeded_db.port, dbname=seeded_db.name, user=user, password=password
    )
    with TrafficTrap() as trap:
        worker = worker_factory(trap.url, "--once", LEDGEROPS_WORKER_DSN=dsn)
        events = worker.wait(expected=2)
        assert not any(row["event"] == "worker_ready" for row in events)
        assert events[0]["event"] == (
            "worker_database_error" if role == "bad_password" else "configuration_error"
        )
        assert events[-1]["event"] == "worker_stopped"
        assert trap.requests == []
    assert payout_state(seeded_db) == before
    with seeded_db.connect("admin") as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
            "AND application_name='ledgerops-worker'",
            (seeded_db.name,),
        ).fetchone() == (0,)


def test_startup_connection_enforces_actual_postgresql_role_and_transaction_time_bounds(
    seeded_db, payment_provider, worker_factory
):
    from ledgerops.worker import WorkerConfig, _connect

    worker = worker_factory(payment_provider.ready["url"], "--once")
    worker.wait()
    config = WorkerConfig.from_env(worker.environment)
    connection, pid = _connect(config)
    with connection:
        assert connection.info.backend_pid == pid
        assert connection.execute("SELECT current_user,session_user").fetchone() == (
            "ledger_worker",
            "ledger_worker",
        )
        for setting, expected in (
            ("statement_timeout", "10s"),
            ("lock_timeout", "5s"),
            ("transaction_timeout", "30s"),
            ("idle_in_transaction_session_timeout", "15s"),
        ):
            assert connection.execute("SELECT current_setting(%s)", (setting,)).fetchone() == (
                expected,
            )
        assert connection.info.transaction_status == TransactionStatus.IDLE
    with seeded_db.connect("admin") as conn:
        eventually(
            lambda: conn.execute(
                "SELECT count(*)=0 FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()[0],
            "startup connection was not closed",
        )


def test_bounded_transport_failures_remain_unresolved_without_fabricating_a_terminal(
    seeded_db, golden, worker_factory
):
    args, _, _, _ = live_admission(seeded_db, golden)
    with TrafficTrap() as trap:
        trap.release.set()  # An actual HTTP 503, with no forwarding or financial effect.
        worker = worker_factory(trap.url, "--max-calls", "2")
        events = worker.wait()
        attempts = [row for row in events if row["event"] == "payout_attempt"]
        assert len(attempts) == len(trap.requests) == 2
        assert all(row["http_status"] == 503 for row in attempts)
        assert events[-1]["completed_calls"] == 2
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
        assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.payout_incident").fetchone() == (0,)
    assert report.healthy and not report.external_evidence["payouts_resolved"]
    assert report.external_evidence["unresolved_payout_ids"] == [str(args[1].operation_id)]


def test_idle_sigterm_closes_owned_database_connection_and_logs_no_secrets(
    seeded_db, payment_provider, worker_factory
):
    worker = worker_factory(payment_provider.ready["url"])
    worker.ready()
    worker.process.send_signal(signal.SIGTERM)
    events = worker.wait()
    assert events[-1]["event"] == "worker_stopped"
    assert events[-1]["completed_calls"] == 0
    assert effect_rows(payment_provider.path) == []


@pytest.mark.parametrize("release_response", [True, False])
def test_sigterm_during_provider_call_finishes_bounded_current_call_without_new_claims(
    seeded_db, golden, payment_provider, worker_factory, release_response
):
    args, _, key, _ = live_admission(seeded_db, golden)
    provider = payment_provider
    assert (
        provider.control("arm", {"provider_key": key, "point": "after_commit", "action": "pause"})[
            0
        ]
        == 200
    )
    worker = worker_factory(provider.ready["url"])
    worker.ready()
    wait_for_gate(provider)
    assert_durable_claim(seeded_db, golden, worker)
    worker.process.send_signal(signal.SIGTERM)
    if release_response:
        assert provider.control("release", {})[0] == 200
    events = worker.wait()
    assert events[-1]["event"] == "worker_stopped"
    assert events[-1]["completed_calls"] == 1
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (1,)
    if release_response:
        check_final(seeded_db, golden, args[1], "SUCCEEDED")
    else:
        assert money(seeded_db, golden)[:3] == (80, 62, 18)
        assert provider.control("release", {})[0] == 200
    assert effect_rows(provider.path) == [(key, "60", "2", "62")]


def test_namespace_selector_does_not_claim_or_transmit_another_providers_work(
    seeded_db, golden, worker_factory
):
    live_admission(seeded_db, golden)
    before = payout_state(seeded_db)
    with TrafficTrap() as trap:
        worker_factory(trap.url, "--once", LEDGEROPS_PROVIDER_NAMESPACE=str(uuid4())).wait()
        assert trap.requests == []
    assert payout_state(seeded_db) == before


def test_active_hold_projection_repair_then_actual_worker_success_preserves_repair_history(
    seeded_db, golden, payment_provider, worker_factory, tmp_path
):
    args, _, key, _ = live_admission(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=90,reserved=12 WHERE account_id=%s",
            (str(args[1].merchant_account_id),),
        )
    with seeded_db.connect("reader") as conn:
        before = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
    assert not before.projection_ok and before.rebuild_eligible
    freeze = _freeze(seeded_db, golden)
    _preview(seeded_db, golden, freeze, tmp_path)
    _apply(seeded_db, golden, freeze)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    with seeded_db.connect("maintenance") as conn:
        resume_workspace(conn, golden["workspace"]["id"], freeze, ACTOR, "active hold repaired")
    worker_factory(payment_provider.ready["url"], "--max-calls", "1").wait()
    check_final(seeded_db, golden, args[1], "SUCCEEDED")
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT before_reserved,after_reserved FROM ledger.projection_repair_row "
            "WHERE account_id=%s",
            (str(args[1].merchant_account_id),),
        ).fetchone() == (12, 62)
    assert_export_and_effect(
        seeded_db, golden, payment_provider, key, tmp_path / "exports", state="SUCCEEDED"
    )
