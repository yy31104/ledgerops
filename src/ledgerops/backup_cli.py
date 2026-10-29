"""Trusted administrator backup/restore tooling; never opens runtime traffic or dispatch."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg

from ledgerops.backup import (
    BackupError,
    PgTools,
    create_backup,
    inspect_backup,
    restore_backup,
)
from ledgerops.domain import ProviderNamespaceId
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.provider_protocol import InvalidObservation, ProviderTrust
from ledgerops.reconciliation.publication import ObjectStore
from ledgerops.recovery import capture_history, reconcile_restored_history, save_history
from ledgerops.worker import ConfigurationError, _read_keys


def _tools(conn):
    params = conn.info.get_parameters()
    keys = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    environment = {target: params[source] for source, target in keys.items() if params.get(source)}
    if conn.info.password:
        environment["PGPASSWORD"] = conn.info.password
    return PgTools(environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--objects", type=Path, required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("backup", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--database", required=True)
    restore.add_argument("--objects", type=Path, required=True)
    restore.add_argument("--restore-id", type=UUID, required=True)
    restore.add_argument("--admin-actor", type=UUID, required=True)
    restore.add_argument("--reason", required=True)
    history = commands.add_parser("history")
    history.add_argument("--provider-url", required=True)
    history.add_argument("--namespace", required=True)
    history.add_argument("--keys-file", type=Path, required=True)
    history.add_argument("--history-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_backup(args.backup)
        else:
            dsn = os.environ.get("LEDGEROPS_BACKUP_ADMIN_DSN")
            if not dsn:
                raise BackupError("BACKUP_ADMIN_DSN_REQUIRED")
            with psycopg.connect(
                dsn,
                autocommit=True,
                connect_timeout=5,
                options="-c timezone=UTC -c statement_timeout=120000 -c lock_timeout=5000",
            ) as conn:
                if args.command == "backup":
                    result = create_backup(
                        conn, _tools(conn), ObjectStore(args.objects), args.destination
                    )
                elif args.command == "restore":
                    result = restore_backup(
                        conn,
                        _tools(conn),
                        args.backup,
                        new_database=args.database,
                        destination_objects=args.objects,
                        restore_id=args.restore_id,
                        admin_actor_id=args.admin_actor,
                        reason=args.reason,
                    )
                else:
                    trust = ProviderTrust(
                        ProviderNamespaceId.parse(args.namespace),
                        _read_keys(args.keys_file),
                    )
                    client = ProviderHTTPClient(
                        args.provider_url, os.environ.get("LEDGEROPS_PROVIDER_TOKEN")
                    )

                    def fetch(**kwargs):
                        response = client.history(**kwargs)
                        if response.http_status != 200:
                            raise BackupError("HISTORY_HTTP_FAILED")
                        return response.body

                    def lookup(key):
                        response = client.lookup(key)
                        if response.http_status != 200:
                            raise BackupError("LOOKUP_HTTP_FAILED")
                        return response.body

                    snapshot = capture_history(fetch, trust=trust)
                    history_ref = save_history(snapshot, args.history_file)
                    result = reconcile_restored_history(conn, snapshot, trust=trust, lookup=lookup)
                    result["retained_history"] = history_ref
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except (
        BackupError,
        InvalidObservation,
        ConfigurationError,
        OSError,
        ValueError,
        psycopg.Error,
    ) as exc:
        print(
            json.dumps({"status": "ERROR", "code": getattr(exc, "code", type(exc).__name__)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
