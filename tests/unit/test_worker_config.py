"""Worker configuration is fail-closed and never prints secret-bearing errors."""

import json
import os
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.pq import TransactionStatus

from ledgerops import worker
from ledgerops.db.admission import InvariantViolation, RetrySameIdentity

SECRET = b"worker-unit-signing-secret-at-least32bytes"
TOKEN = "worker-unit-command-token-never-log-me"
DSN = "host=127.0.0.1 port=5432 dbname=ledger user=ledger_worker password=private-db-password"
NAMESPACE = "70000000-0000-4000-8000-000000000001"


@pytest.fixture
def environment(tmp_path):
    keyfile = tmp_path / "private-provider-keys.json"
    keyfile.write_text(json.dumps({"local-v1": SECRET.hex()}))
    keyfile.chmod(0o600)
    return {
        "LEDGEROPS_RESTORE_MODE": "false",
        "LEDGEROPS_DISPATCH_ENABLED": "true",
        "LEDGEROPS_WORKER_DSN": DSN,
        "LEDGEROPS_PROVIDER_URL": "http://127.0.0.1:12345",
        "LEDGEROPS_PROVIDER_TOKEN": TOKEN,
        "LEDGEROPS_PROVIDER_NAMESPACE": NAMESPACE,
        "LEDGEROPS_PROVIDER_KEYS_FILE": str(keyfile),
    }


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_explicit_valid_config_copies_protected_keys_and_hides_secrets(environment):
    config = worker.WorkerConfig.from_env(environment)
    assert config.trust.keys["local-v1"] == SECRET
    assert str(config.trust.namespace) == NAMESPACE
    assert (config.batch_size, config.poll_milliseconds, config.lease_seconds) == (20, 1000, 30)
    assert config.provider_timeout_milliseconds == 5000
    for value in (TOKEN, DSN, SECRET.decode(), SECRET.hex()):
        assert value not in repr(config)


@pytest.mark.parametrize("restore", [None, "true", "TRUE", "False", "", False])
def test_restore_mode_defaults_to_no_dispatch_even_if_enable_is_present(environment, restore):
    if restore is None:
        del environment["LEDGEROPS_RESTORE_MODE"]
    else:
        environment["LEDGEROPS_RESTORE_MODE"] = restore
    with pytest.raises(worker.ConfigurationError, match="RESTORE_MODE_DISPATCH_DISABLED"):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize("enabled", [None, "false", "TRUE", "", True])
def test_dispatch_requires_exact_explicit_enable(environment, enabled):
    if enabled is None:
        del environment["LEDGEROPS_DISPATCH_ENABLED"]
    else:
        environment["LEDGEROPS_DISPATCH_ENABLED"] = enabled
    with pytest.raises(worker.ConfigurationError, match="EXPLICIT_DISPATCH_ENABLE_REQUIRED"):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize(
    "name",
    [
        "LEDGEROPS_WORKER_DSN",
        "LEDGEROPS_PROVIDER_URL",
        "LEDGEROPS_PROVIDER_TOKEN",
        "LEDGEROPS_PROVIDER_NAMESPACE",
        "LEDGEROPS_PROVIDER_KEYS_FILE",
    ],
)
def test_missing_required_configuration_does_not_fall_back_to_test_defaults(environment, name):
    del environment[name]
    with pytest.raises(worker.ConfigurationError, match="REQUIRED_WORKER_CONFIGURATION_MISSING"):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize(
    "name,value",
    [
        ("LEDGEROPS_WORKER_BATCH_SIZE", "0"),
        ("LEDGEROPS_WORKER_BATCH_SIZE", "101"),
        ("LEDGEROPS_WORKER_POLL_MS", "49"),
        ("LEDGEROPS_WORKER_POLL_MS", "60001"),
        ("LEDGEROPS_WORKER_LEASE_SECONDS", "0"),
        ("LEDGEROPS_WORKER_LEASE_SECONDS", "301"),
        ("LEDGEROPS_PROVIDER_TIMEOUT_MS", "0"),
        ("LEDGEROPS_PROVIDER_TIMEOUT_MS", "30001"),
        ("LEDGEROPS_WORKER_BATCH_SIZE", "9" * 10000),
        ("LEDGEROPS_WORKER_BATCH_SIZE", "１"),
        ("LEDGEROPS_WORKER_BATCH_SIZE", "-1"),
        ("LEDGEROPS_WORKER_BATCH_SIZE", "1.5"),
    ],
)
def test_numeric_resource_limits_are_bounded_before_conversion(environment, name, value):
    environment[name] = value
    with pytest.raises(worker.ConfigurationError):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"{}",
        b'{"x":"zz"}',
        b'{"x":true}',
        b'{"x":null}',
        b'{"x":"' + b"aa" * 32 + b'","x":"' + b"bb" * 32 + b'"}',
        b"\xff",
        b"x" * 16385,
    ],
)
def test_keyfile_requires_bounded_unique_hexadecimal_keys(environment, raw):
    with open(environment["LEDGEROPS_PROVIDER_KEYS_FILE"], "wb") as stream:
        stream.write(raw)
    with pytest.raises(worker.ConfigurationError, match="INVALID_PROTECTED_PROVIDER_KEYS"):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_group_or_world_readable_keyfiles_are_refused(environment, mode):
    os.chmod(environment["LEDGEROPS_PROVIDER_KEYS_FILE"], mode)
    with pytest.raises(worker.ConfigurationError, match="INVALID_PROTECTED_PROVIDER_KEYS"):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_nonregular_or_symlink_keyfiles_are_refused_without_blocking(environment, tmp_path, kind):
    path = tmp_path / "alternate-secret-source"
    if kind == "symlink":
        path.symlink_to(environment["LEDGEROPS_PROVIDER_KEYS_FILE"])
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o700)
    environment["LEDGEROPS_PROVIDER_KEYS_FILE"] = str(path)
    with pytest.raises(worker.ConfigurationError, match="INVALID_PROTECTED_PROVIDER_KEYS"):
        worker.WorkerConfig.from_env(environment)


def test_keyfile_must_belong_to_effective_process_user(environment, monkeypatch):
    actual_uid = os.getuid()
    monkeypatch.setattr(worker.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(worker.ConfigurationError):
        worker.WorkerConfig.from_env(environment)


@pytest.mark.parametrize(
    "changes",
    [
        {"dsn": "private-bad-dsn"},
        {"provider_url": "http://remote.invalid:80"},
        {"provider_token": "short"},
        {"batch_size": True},
        {"lease_seconds": 301},
        {"provider_timeout_milliseconds": 0},
    ],
)
def test_direct_typed_config_cannot_bypass_validation(environment, changes):
    config = worker.WorkerConfig.from_env(environment)
    with pytest.raises(worker.ConfigurationError):
        replace(config, **changes)


class Connection:
    def __init__(self):
        self.closed = False
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    def close(self):
        self.closed = True


def test_connection_sets_limits_for_whole_transactions_and_individual_statements(
    environment, monkeypatch
):
    conn = Connection()
    received = []

    def connect(*args, **kwargs):
        received.append((args, kwargs))
        return conn

    monkeypatch.setattr(worker.psycopg, "connect", connect)
    monkeypatch.setattr(
        worker, "_validate_database", lambda actual: 1234 if actual is conn else None
    )
    assert worker._connect(worker.WorkerConfig.from_env(environment)) == (conn, 1234)
    args, kwargs = received[0]
    assert args == (DSN,)
    assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 3
    options = kwargs["options"].split("-c ")[1:]
    assert {value.strip() for value in options} == {
        "timezone=UTC",
        "statement_timeout=10000",
        "lock_timeout=5000",
        "transaction_timeout=30000",
        "idle_in_transaction_session_timeout=15000",
    }


@pytest.mark.parametrize("retry", [True, False])
def test_unprocessed_candidates_back_off_and_shutdown_can_interrupt_wait(
    environment, monkeypatch, capsys, retry
):
    conn = Connection()
    stop = Event()
    waits = []
    monkeypatch.setattr(worker, "_connect", lambda _: (conn, 1234))
    monkeypatch.setattr(worker, "due_payouts", lambda *_args, **_kwargs: [(uuid4(), uuid4())])

    def dispatch(*_args, **_kwargs):
        if retry:
            raise RetrySameIdentity("database_contention")
        return None

    def wait(timeout):
        waits.append(timeout)
        stop.set()
        return True

    monkeypatch.setattr(worker, "dispatch_once", dispatch)
    monkeypatch.setattr(stop, "wait", wait)
    config = replace(worker.WorkerConfig.from_env(environment), poll_milliseconds=50)
    assert worker.run(config, stop) == 0
    assert waits == [1.0] and conn.closed
    assert events(capsys)[-1]["event"] == "worker_stopped"


def test_no_ready_signal_before_a_successful_database_validation(environment, monkeypatch, capsys):
    def fail_connect(_):
        raise psycopg.OperationalError(DSN + TOKEN + SECRET.hex())

    monkeypatch.setattr(worker, "_connect", fail_connect)
    assert worker.run(worker.WorkerConfig.from_env(environment), Event()) == 2
    output = capsys.readouterr().out
    rows = [json.loads(line) for line in output.splitlines()]
    assert [row["event"] for row in rows] == ["worker_database_error", "worker_stopped"]
    for value in (DSN, TOKEN, SECRET.hex()):
        assert value not in output


@pytest.mark.parametrize(
    "failure",
    [
        InvariantViolation("private-raw-invariant"),
        errors.CheckViolation("private-raw-sql-error"),
    ],
)
def test_data_invariants_and_sql_integrity_errors_fail_stop_instead_of_hot_retrying(
    environment, monkeypatch, capsys, failure
):
    conn = Connection()
    monkeypatch.setattr(worker, "_connect", lambda _: (conn, 1234))
    monkeypatch.setattr(worker, "due_payouts", lambda *_args, **_kwargs: [(uuid4(), uuid4())])
    calls = []

    def fail_dispatch(*_args, **_kwargs):
        calls.append(1)
        raise failure

    monkeypatch.setattr(worker, "dispatch_once", fail_dispatch)
    assert worker.run(worker.WorkerConfig.from_env(environment), Event()) == 2
    rows = events(capsys)
    assert len(calls) == 1 and conn.closed
    assert rows[0]["event"] == "worker_ready" and rows[-1]["event"] == "worker_stopped"
    assert all(row["event"] != "worker_internal_error" for row in rows)
    assert str(failure) not in json.dumps(rows)


def test_unknown_retry_reason_is_sanitized_and_once_reports_unprocessed_batch(
    environment, monkeypatch, capsys
):
    conn = Connection()
    monkeypatch.setattr(worker, "_connect", lambda _: (conn, 1234))
    monkeypatch.setattr(worker, "due_payouts", lambda *_args, **_kwargs: [(uuid4(), uuid4())])

    def retry(*_args, **_kwargs):
        raise RetrySameIdentity("must-not-print-private-reason")

    monkeypatch.setattr(worker, "dispatch_once", retry)
    assert worker.run(worker.WorkerConfig.from_env(environment), Event(), once=True) == 2
    rows = events(capsys)
    blocked = next(row for row in rows if row["event"] == "payout_blocked")
    assert blocked["reason"] == "retry_same_identity"
    assert "must-not-print-private-reason" not in json.dumps(rows)
    assert conn.closed


def test_empty_once_scan_is_ready_then_idle_then_clean_stop(environment, monkeypatch, capsys):
    conn = Connection()
    config = worker.WorkerConfig.from_env(environment)
    monkeypatch.setattr(worker, "_connect", lambda _: (conn, 1234))
    received = []

    def due(actual, **kwargs):
        assert actual is conn
        received.append(kwargs)
        return []

    monkeypatch.setattr(worker, "due_payouts", due)
    assert worker.run(config, Event(), once=True) == 0
    assert received == [{"limit": 20, "provider_namespace": config.trust.namespace}]
    assert [row["event"] for row in events(capsys)] == [
        "worker_ready",
        "worker_idle",
        "worker_stopped",
    ]
    assert conn.closed


def test_restore_failure_exits_cli_before_opening_database_or_network(
    environment, monkeypatch, capsys
):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LEDGEROPS_RESTORE_MODE", raising=False)
    monkeypatch.setattr(worker, "_connect", lambda _: pytest.fail("restore must not connect"))
    monkeypatch.setattr(
        worker,
        "ProviderHTTPClient",
        lambda *_args, **_kwargs: pytest.fail("restore must not create transport"),
    )
    assert worker.main(["--once"]) == 2
    assert events(capsys) == [
        {"event": "configuration_error", "reason": "RESTORE_MODE_DISPATCH_DISABLED"}
    ]


@pytest.mark.parametrize("value", ["0", "-1", "10001"])
def test_cli_max_calls_requires_a_bounded_positive_integer(value):
    with pytest.raises(SystemExit) as raised:
        worker.main(["--max-calls", value])
    assert raised.value.code == 2
