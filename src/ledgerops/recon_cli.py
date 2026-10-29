"""Bounded offline reconciliation and immutable local evidence publication."""

import argparse
import json
import os
import signal
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict

from ledgerops.cli import _code_version
from ledgerops.export import load_export_bundle
from ledgerops.reconciliation import StatementTrust, reconcile_bundle
from ledgerops.reconciliation.publication import (
    ObjectStore,
    PublicationError,
    pin_run,
    release_reader_pin,
)
from ledgerops.reconciliation.runner import RunError
from ledgerops.reconciliation.statement import MAX_BYTES, CorruptInput
from ledgerops.worker import _read_keys


def _statement_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise CorruptInput("INVALID_STATEMENT_FILE")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CorruptInput("STATEMENT_BYTE_LIMIT")
        return raw
    finally:
        os.close(descriptor)


def _connect(role):
    dsn = os.environ.get("LEDGEROPS_RECON_DSN", "")
    if not 1 <= len(dsn) <= 16384:
        raise ValueError("reconciliation DSN required")
    conninfo_to_dict(dsn)
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=3,
        application_name="ledgerops-reconciliation",
        options=(
            "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
            "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
        ),
    )
    try:
        row = conn.execute(
            "SELECT r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,r.rolreplication,"
            "pg_has_role(current_user,%s,'USAGE'),"
            "pg_has_role(current_user,'ledger_migrator','MEMBER') "
            "FROM pg_roles r WHERE r.rolname=current_user",
            (role,),
        ).fetchone()
        if row is None or any(row[:5]) or not row[5] or row[6]:
            raise ValueError("dedicated unprivileged evidence role required")
        conn.execute("SELECT evidence_epoch FROM ledger.recon_registry_guard LIMIT 0")
        conn.execute("SELECT state,manifest,partition_plan FROM ledger.recon_run LIMIT 0")
        return conn
    except BaseException:
        conn.close()
        raise


def _input_arguments(command):
    command.add_argument("--ledger-export", type=Path, required=True)
    command.add_argument("--statement", type=Path, required=True)
    command.add_argument("--namespace", required=True)
    command.add_argument("--keys-file", type=Path, required=True)
    command.add_argument(
        "--code-revision", help="Full matcher Git revision; required outside checkout"
    )
    command.add_argument("--dirty", action="store_true")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _input_arguments(
        commands.add_parser("match", help="Compare fixed local files without a database")
    )
    publish = commands.add_parser("publish", help="Compute and publish one complete immutable run")
    _input_arguments(publish)
    publish.add_argument("--objects", type=Path, required=True)
    publish.add_argument("--workers", type=int, choices=(1, 2, 4), default=1)
    publish.add_argument("--partitions", type=int, default=16)
    publish.add_argument("--timeout-seconds", type=int, default=300)
    publish.add_argument("--run-id", help="Stable UUID for inspecting an interrupted attempt")
    read = commands.add_parser(
        "read", help="Pin and read a COMPLETE run; release its reader pin on exit"
    )
    read.add_argument("--objects", type=Path, required=True)
    read.add_argument("--workspace", required=True)
    selection = read.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-id")
    selection.add_argument("--current", action="store_true")
    read.add_argument("--namespace")
    read.add_argument("--date")
    release = commands.add_parser(
        "release-pin", help="Release a named reader pin after its reader stopped"
    )
    release.add_argument("--workspace", required=True)
    release.add_argument("--reader-id", required=True)
    return parser


def _print(value):
    print(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")), flush=True)


@contextmanager
def _interrupt_on_termination():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _read(args):
    if args.current and (not args.namespace or not args.date):
        raise ValueError("current selection requires namespace and date")
    store, reader = ObjectStore(args.objects), uuid4()
    with _connect("ledger_reader") as conn:
        try:
            pinned = pin_run(
                conn,
                args.workspace,
                store,
                reader_id=reader,
                run_id=args.run_id,
                provider_namespace=args.namespace,
                business_date=args.date,
            )
            _print(asdict(pinned))
            return 0 if pinned.result["reconciled"] else 1
        finally:
            # Explicitly release even if a pinned file proves corrupt. If the
            # connection itself fails, the printed reader ID permits cleanup.
            print(json.dumps({"reader_id": str(reader)}), file=sys.stderr)
            if not conn.closed:
                release_reader_pin(conn, args.workspace, reader_id=reader)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "read":
            return _read(args)
        if args.command == "release-pin":
            with _connect("ledger_reader") as conn:
                _print(
                    {"released": release_reader_pin(conn, args.workspace, reader_id=args.reader_id)}
                )
            return 0
        revision, dirty = _code_version(args)
        trust = StatementTrust(args.namespace, _read_keys(args.keys_file))
        statement = _statement_bytes(args.statement)
        if args.command == "match":
            report = reconcile_bundle(
                args.ledger_export,
                statement,
                trust=trust,
                code_revision=revision,
                dirty=dirty,
            )
            _print(report)
            return 2 if not report["usable"] else 0 if report["reconciled"] else 1
        from ledgerops.reconciliation.runner import run_reconciliation

        store, bundle = ObjectStore(args.objects), load_export_bundle(args.ledger_export)
        with _connect("ledger_reconciler") as conn, _interrupt_on_termination():
            result = run_reconciliation(
                conn,
                store,
                bundle,
                statement,
                trust=trust,
                code_revision=revision,
                dirty=dirty,
                workers=args.workers,
                partitions=args.partitions,
                timeout_seconds=args.timeout_seconds,
                run_id=args.run_id,
            )
        _print(asdict(result))
        # Publication success is distinct from a report containing differences.
        # Inspect the immutable report with read; publish does not claim healthy.
        return 0
    except RunError as exc:
        print(
            json.dumps(
                {
                    "error": exc.code,
                    "run_id": str(exc.run_id),
                    "failure_recorded": exc.failure_recorded,
                    "outcome_unknown": exc.outcome_unknown,
                    "metrics": exc.metrics,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, psycopg.Error, PublicationError):
        print(
            json.dumps({"error": "RECONCILIATION_INPUT_OR_CONFIGURATION_INVALID"}), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
