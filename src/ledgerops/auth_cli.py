"""Explicit credential administration with a separate privileged database identity."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

from ledgerops import authentication as auth


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("membership", "client", "issue", "revoke"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--workspace", required=True)
        cmd.add_argument("--admin-actor", required=True)
        cmd.add_argument("--reason", required=True)
        if name in ("membership", "client"):
            cmd.add_argument("--actor", required=True)
            cmd.add_argument("--disable", action="store_true")
            cmd.add_argument("--expected-version", type=int)
        if name in ("client", "issue"):
            cmd.add_argument("--client", required=True)
        if name in ("membership", "issue"):
            cmd.add_argument("--scope", action="append", default=[])
        if name == "issue":
            cmd.add_argument("--ttl-seconds", type=int, default=3600)
            cmd.add_argument("--output", type=Path, required=True)
        if name == "revoke":
            cmd.add_argument("--credential", required=True)
    return parser


def _connect():
    dsn = os.environ.get("LEDGEROPS_AUTH_ADMIN_DSN", "")
    if not 1 <= len(dsn) <= 16384:
        raise ValueError("administrator configuration required")
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=3,
        application_name="ledgerops-auth-admin",
        options=(
            "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 "
            "-c transaction_timeout=30000 -c idle_in_transaction_session_timeout=15000"
        ),
    )
    try:
        row = conn.execute(
            "SELECT r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,r.rolreplication,"
            "pg_has_role(current_user,'ledger_auth_admin','USAGE') "
            "FROM pg_roles r WHERE r.rolname=current_user"
        ).fetchone()
        if row is None or any(row[:5]) or not row[5]:
            raise ValueError("dedicated identity administrator required")
        return conn
    except BaseException:
        conn.close()
        raise


def _issue(conn, args, common):
    # Reserve an owner-only path before creating a credential. An interruption
    # after DB COMMIT may leave an inaccessible credential; revoke its ID before
    # retrying. No default token or plaintext credential goes to stdout/logs.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    issued = None
    try:
        issued = auth.issue_credential(
            conn, args.workspace, args.client, args.scope, ttl_seconds=args.ttl_seconds, **common
        )
        raw = json.dumps(
            {
                "token": issued.token,
                "credential_id": str(issued.credential_id),
                "expires_at": issued.expires_at.isoformat(),
                "generation": str(issued.generation),
            },
            separators=(",", ":"),
        ).encode()
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "credential_id": str(issued.credential_id),
            "expires_at": issued.expires_at.isoformat(),
            "secret_file": str(args.output.absolute()),
        }
    except BaseException:
        if issued is not None and not conn.closed:
            auth.revoke_credential(conn, args.workspace, str(issued.credential_id), **common)
        args.output.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv=None):
    args = _parser().parse_args(argv)
    common = {"admin_actor_id": args.admin_actor, "reason": args.reason}
    try:
        with _connect() as conn:
            if args.command == "membership":
                result = auth.set_membership(
                    conn,
                    args.workspace,
                    args.actor,
                    args.scope,
                    enabled=not args.disable,
                    expected_version=args.expected_version,
                    **common,
                )
            elif args.command == "client":
                result = auth.set_client(
                    conn,
                    args.workspace,
                    args.client,
                    args.actor,
                    enabled=not args.disable,
                    expected_version=args.expected_version,
                    **common,
                )
            elif args.command == "issue":
                result = _issue(conn, args, common)
            else:
                result = auth.revoke_credential(conn, args.workspace, args.credential, **common)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except (OSError, ValueError, psycopg.Error):
        print(json.dumps({"error": "IDENTITY_ADMINISTRATION_FAILED"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
