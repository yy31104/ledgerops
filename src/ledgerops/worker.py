"""Bounded payout worker for the explicit local synthetic-provider adapter."""

import argparse
import json
import os
import re
import signal
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import InvariantViolation, RetrySameIdentity
from ledgerops.db.payout_dispatch import dispatch_once, due_payouts
from ledgerops.domain import ProviderNamespaceId
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.provider_protocol import ProviderTrust


class ConfigurationError(ValueError):
    """A stable configuration code, never the raw secret-bearing parser error."""


def _integer(env, name, default, minimum, maximum):
    raw = env.get(name, str(default))
    if (
        type(raw) is not str
        or len(raw) > 6
        or not raw.isascii()
        or not raw.isdecimal()
        or not minimum <= int(raw) <= maximum
    ):
        raise ConfigurationError(f"INVALID_{name}")
    return int(raw)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_keys(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
            or not 1 <= metadata.st_size <= 16384
        ):
            raise ValueError("invalid protected key file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("key file grew beyond its bound")
        keys = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if type(keys) is not dict or not 1 <= len(keys) <= 8:
            raise ValueError("key file must contain one to eight keys")
        if any(
            type(value) is not str
            or not re.fullmatch(r"[0-9a-fA-F]{64,1024}", value)
            or len(value) % 2
            for value in keys.values()
        ):
            raise ValueError("key material must be bounded hexadecimal text")
        return {name: bytes.fromhex(value) for name, value in keys.items()}
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ConfigurationError("INVALID_PROTECTED_PROVIDER_KEYS") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True, repr=False)
class WorkerConfig:
    dsn: str = field(repr=False)
    provider_url: str
    provider_token: str = field(repr=False)
    trust: ProviderTrust = field(repr=False)
    batch_size: int = 20
    poll_milliseconds: int = 1000
    lease_seconds: int = 30
    provider_timeout_milliseconds: int = 5000

    def __post_init__(self):
        if type(self.dsn) is not str or not 1 <= len(self.dsn) <= 16384:
            raise ConfigurationError("INVALID_WORKER_DSN")
        try:
            conninfo_to_dict(self.dsn)
        except (psycopg.Error, ValueError) as exc:
            raise ConfigurationError("INVALID_WORKER_DSN") from exc
        if type(self.trust) is not ProviderTrust:
            raise ConfigurationError("INVALID_PROVIDER_TRUST")
        for name, lower, upper in (
            ("batch_size", 1, 100),
            ("poll_milliseconds", 50, 60000),
            ("lease_seconds", 1, 300),
            ("provider_timeout_milliseconds", 1, 30000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ConfigurationError("INVALID_WORKER_LIMITS")
        if self.lease_seconds * 1000 < self.provider_timeout_milliseconds + 5000:
            raise ConfigurationError("LEASE_SHORTER_THAN_PROVIDER_TIMEOUT_MARGIN")
        try:
            ProviderHTTPClient(
                self.provider_url,
                self.provider_token,
                timeout=self.provider_timeout_milliseconds / 1000,
            )
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("INVALID_SYNTHETIC_PROVIDER_CONFIGURATION") from exc

    @classmethod
    def from_env(cls, env):
        # Independent of restored database flags: a stale NORMAL backup must not
        # enable sending unless the operator explicitly authorizes this process.
        if env.get("LEDGEROPS_RESTORE_MODE", "true") != "false":
            raise ConfigurationError("RESTORE_MODE_DISPATCH_DISABLED")
        if env.get("LEDGEROPS_DISPATCH_ENABLED") != "true":
            raise ConfigurationError("EXPLICIT_DISPATCH_ENABLE_REQUIRED")
        required = (
            "LEDGEROPS_WORKER_DSN",
            "LEDGEROPS_PROVIDER_URL",
            "LEDGEROPS_PROVIDER_TOKEN",
            "LEDGEROPS_PROVIDER_NAMESPACE",
            "LEDGEROPS_PROVIDER_KEYS_FILE",
        )
        if any(type(env.get(name)) is not str or not env[name] for name in required):
            raise ConfigurationError("REQUIRED_WORKER_CONFIGURATION_MISSING")
        try:
            namespace = ProviderNamespaceId.parse(env["LEDGEROPS_PROVIDER_NAMESPACE"])
            trust = ProviderTrust(namespace, _read_keys(Path(env["LEDGEROPS_PROVIDER_KEYS_FILE"])))
        except ConfigurationError:
            raise
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("INVALID_PROVIDER_TRUST") from exc
        return cls(
            env["LEDGEROPS_WORKER_DSN"],
            env["LEDGEROPS_PROVIDER_URL"],
            env["LEDGEROPS_PROVIDER_TOKEN"],
            trust,
            _integer(env, "LEDGEROPS_WORKER_BATCH_SIZE", 20, 1, 100),
            _integer(env, "LEDGEROPS_WORKER_POLL_MS", 1000, 50, 60000),
            _integer(env, "LEDGEROPS_WORKER_LEASE_SECONDS", 30, 1, 300),
            _integer(env, "LEDGEROPS_PROVIDER_TIMEOUT_MS", 5000, 1, 30000),
        )


def _log(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def _validate_database(conn):
    row = conn.execute(
        "SELECT current_user,pg_backend_pid(),r.rolsuper,r.rolbypassrls,"
        "r.rolcreatedb,r.rolcreaterole,"
        "r.rolreplication,pg_has_role(current_user,'ledger_worker','USAGE'),"
        "pg_has_role(current_user,'ledger_migrator','MEMBER') "
        "FROM pg_roles r WHERE r.rolname=current_user"
    ).fetchone()
    if row is None or any(row[2:7]) or not row[7] or row[8]:
        raise ConfigurationError("DEDICATED_UNPRIVILEGED_WORKER_ROLE_REQUIRED")
    # Verify the actual post-007 capabilities without granting the worker
    # migration ownership or access to a second migration-management interface.
    conn.execute("SELECT knowledge,source_sequence,provider_request FROM ledger.payout LIMIT 0")
    conn.execute("SELECT observation_id,envelope FROM ledger.provider_observation LIMIT 0")
    conn.execute("SELECT observation_id,journal_id FROM ledger.payout_resolution LIMIT 0")
    conn.execute("SELECT related_observation_id,category,code FROM ledger.payout_incident LIMIT 0")
    conn.execute("SELECT next_action,last_attempt_id FROM ledger.dispatch_intent LIMIT 0")
    return row[1]


def _connect(config):
    conn = psycopg.connect(
        config.dsn,
        autocommit=True,
        connect_timeout=3,
        application_name="ledgerops-worker",
        options=(
            "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
            "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
        ),
    )
    try:
        pid = _validate_database(conn)
        return conn, pid
    except Exception:
        conn.close()
        raise


def _retry_reason(exc):
    reasons = {
        "workspace_not_normal",
        "database_contention",
        "transaction_retries_exhausted",
        "dispatch_capacity_unavailable",
        "settlement_capacity_unavailable",
    }
    return exc.reason if exc.reason in reasons else "retry_same_identity"


def _recoverable_database_error(exc):
    state = exc.sqlstate
    if state is None:
        return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
    return state.startswith("08") or state in {
        "57P01",
        "57P02",
        "57P03",
        "53300",
        "40001",
        "40P01",
        "55P03",
        "57014",
    }


def run(config, stop, *, once=False, max_calls=None):
    """Scan bounded batches; fail-stop on trusted-data invariants, never silently skip them."""
    if type(config) is not WorkerConfig or type(once) is not bool:
        raise ConfigurationError("INVALID_WORKER_ARGUMENTS")
    if max_calls is not None and (type(max_calls) is not int or not 1 <= max_calls <= 10000):
        raise ConfigurationError("INVALID_WORKER_MAX_CALLS")
    client = ProviderHTTPClient(
        config.provider_url,
        config.provider_token,
        timeout=config.provider_timeout_milliseconds / 1000,
    )
    calls, failures = 0, 0
    conn = None
    ever_ready, idle_reported = False, False
    try:
        while not stop.is_set():
            had_candidates, calls_before_scan = False, calls
            try:
                if conn is None or conn.closed:
                    conn, pid = _connect(config)
                    if stop.is_set():
                        break
                    ever_ready = True
                    _log(
                        "worker_ready",
                        pid=os.getpid(),
                        backend_pid=pid,
                        namespace=str(config.trust.namespace),
                    )
                candidates = due_payouts(
                    conn, limit=config.batch_size, provider_namespace=config.trust.namespace
                )
                had_candidates = bool(candidates)
                if not candidates and not idle_reported:
                    _log("worker_idle", completed_calls=calls, blocked_or_failed=failures)
                    idle_reported = True
                elif candidates:
                    idle_reported = False
                for workspace, payout in candidates:
                    if stop.is_set():
                        break
                    try:
                        result = dispatch_once(
                            conn,
                            workspace,
                            payout,
                            client,
                            trust=config.trust,
                            lease_seconds=config.lease_seconds,
                        )
                        if result is not None:
                            calls += 1
                            _log(
                                "payout_attempt",
                                workspace_id=str(workspace),
                                payout_id=str(payout),
                                attempt_id=str(result.claim.attempt_id),
                                outcome=result.outcome,
                                http_status=result.http_status,
                                resolution=None
                                if result.resolution is None
                                else result.resolution.status,
                                resolution_code=None
                                if result.resolution is None
                                else result.resolution.code,
                            )
                    except RetrySameIdentity as exc:
                        failures += 1
                        _log(
                            "payout_blocked",
                            workspace_id=str(workspace),
                            payout_id=str(payout),
                            reason=_retry_reason(exc),
                        )
                    except InvariantViolation:
                        failures += 1
                        _log(
                            "worker_invariant_error",
                            workspace_id=str(workspace),
                            payout_id=str(payout),
                            category="InvariantViolation",
                        )
                        return 2
                    if max_calls is not None and calls >= max_calls:
                        return 0
                if candidates:
                    _log(
                        "worker_scan",
                        candidate_count=len(candidates),
                        completed_calls=calls,
                        blocked_or_failed=failures,
                    )
                if once:
                    return 0 if failures == 0 else 2
            except ConfigurationError as exc:
                failures += 1
                _log("configuration_error", reason=str(exc))
                return 2
            except psycopg.Error as exc:
                failures += 1
                _log("worker_database_error", category=type(exc).__name__)
                if conn is not None:
                    conn.close()
                    conn = None
                if not ever_ready or once or not _recoverable_database_error(exc):
                    return 2
            if conn is not None and conn.info.transaction_status != TransactionStatus.IDLE:
                raise InvariantViolation("worker left a transaction outside dispatch")
            # Contended/capacity-blocked queue heads must not turn a short
            # polling interval into a hot retry loop. Shutdown can interrupt
            # this wait, while an active bounded DB/HTTP chain finishes first.
            pause = config.poll_milliseconds / 1000
            if had_candidates and calls == calls_before_scan:
                pause = max(pause, 1.0)
            stop.wait(pause)
        return 0
    except Exception as exc:
        failures += 1
        _log("worker_internal_error", category=type(exc).__name__)
        return 2
    finally:
        if conn is not None:
            conn.close()
        _log("worker_stopped", completed_calls=calls, blocked_or_failed=failures)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="scan one bounded batch and exit")
    parser.add_argument(
        "--max-calls", type=int, help="exit after this many completed dispatch attempts"
    )
    args = parser.parse_args(argv)
    if args.max_calls is not None and not 1 <= args.max_calls <= 10000:
        parser.error("--max-calls must be between 1 and 10000")
    try:
        config = WorkerConfig.from_env(os.environ)
    except ConfigurationError as exc:
        _log("configuration_error", reason=str(exc))
        return 2
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    return run(config, stop, once=args.once, max_calls=args.max_calls)


if __name__ == "__main__":
    sys.exit(main())
