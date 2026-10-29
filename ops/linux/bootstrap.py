"""Explicit local administrator jobs, separate from every request/worker startup."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from ledgerops import authentication as auth
from ledgerops.db.migrations import apply_migrations
from ledgerops.http.config import bounded_file

UID = 10001
PG_UID = 999
GROUPS = (
    "ledger_migrator",
    "ledger_app",
    "ledger_reader",
    "ledger_maintenance",
    "ledger_worker",
    "ledger_reconciler",
    "ledger_auth_reader",
    "ledger_auth_admin",
    "ledger_operator",
)
LOGINS = {
    "ledger_service": ("ledger_app", "ledger_reader", "ledger_auth_reader"),
    "ledger_operator_http": ("ledger_app", "ledger_reader", "ledger_auth_reader"),
    "ledger_action_operator": ("ledger_operator",),
    "ledger_dispatcher": ("ledger_worker",),
    "ledger_evidence_reader": ("ledger_reader",),
    "ledger_reconciliation": ("ledger_reconciler", "ledger_reader"),
    "ledger_identity_admin": ("ledger_auth_admin",),
    "ledger_schema_admin": ("ledger_migrator",),
    "ledger_maintenance_login": ("ledger_maintenance",),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def write_secret(path, value, *, uid=UID, raw=False):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, uid)
        stream.write(value if raw else canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def dsn(state, role, database="ledgerops"):
    return make_conninfo(
        host="postgres",
        port=5432,
        dbname=database,
        user=role,
        password=state["passwords"][role],
        connect_timeout=3,
    )


def profile(directory, environment):
    write_secret(directory / "profile.json", {"version": 1, "environment": environment})


def initialize_files():
    if os.geteuid() != 0:
        raise ValueError("EXPLICIT_ROOT_VOLUME_INITIALIZER_REQUIRED")
    volumes = Path("/volumes")
    names = (
        "api",
        "worker",
        "provider",
        "operator",
        "bootstrap",
        "backup",
        "reconciler",
        "operator-scratch",
        "bootstrap-scratch",
        "backup-scratch",
        "reconciler-scratch",
        "evidence-inputs",
        "postgres",
        "postgres-data",
        "provider-data",
        "objects",
        "backups",
        "caddy",
    )
    for name in names:
        path = volumes / name
        path.mkdir(exist_ok=True)
        owner = PG_UID if name in {"postgres", "postgres-data"} else UID
        os.chown(path, owner, owner)
        os.chmod(path, 0o700)
    state_file = volumes / "bootstrap" / "state.json"
    if state_file.exists():
        print('{"event":"owned_secret_volumes_already_initialized"}')
        return
    state = {
        name: str(uuid4())
        for name in (
            "deployment_id",
            "namespace",
            "workspace_id",
            "system_actor",
            "customer_actor",
            "merchant_actor",
            "maker_actor",
            "checker_actor",
            "admin_actor",
            "customer_account",
            "merchant_account",
            "clearing_account",
            "beneficiary_id",
            "customer_client",
            "merchant_client",
            "maker_client",
            "checker_client",
        )
    }
    state["passwords"] = {name: secrets.token_urlsafe(32) for name in (*LOGINS, "postgres")}
    state["provider_hmac"] = secrets.token_hex(32)
    state["provider_token"] = secrets.token_urlsafe(32)
    policy = {
        "version": 1,
        "workspaces": [
            {
                "workspace_id": state["workspace_id"],
                "provider_namespace": state["namespace"],
                "fixed_fee_minor": 2,
                "dispatch_ttl_seconds": 60,
                "beneficiary_owners": {state["beneficiary_id"]: state["merchant_actor"]},
            }
        ],
    }
    common = {
        "LEDGEROPS_ENV": "production",
        "LEDGEROPS_TLS_TERMINATED": "true",
        "LEDGEROPS_SERVICE_HOSTS": "localhost,127.0.0.1",
        "LEDGEROPS_SERVICE_BIND": "0.0.0.0:8080",
        "LEDGEROPS_SERVICE_WORKERS": "2",
        "LEDGEROPS_RESTORE_MODE": "true",
        "LEDGEROPS_API_WRITES_ENABLED": "false",
        "LEDGEROPS_TRUSTED_PROXIES": os.environ["LEDGEROPS_PROXY_IP"],
        "LEDGEROPS_PAYOUT_POLICY_FILE": "/run/ledgerops/payout-policy.json",
    }
    profile(volumes / "api", {**common, "LEDGEROPS_SERVICE_DSN": dsn(state, "ledger_service")})
    write_secret(volumes / "api" / "payout-policy.json", policy)
    profile(
        volumes / "operator",
        {
            **common,
            "LEDGEROPS_SERVICE_WORKERS": "1",
            "LEDGEROPS_SERVICE_DSN": dsn(state, "ledger_operator_http"),
            "LEDGEROPS_OPERATOR_DSN": dsn(state, "ledger_action_operator"),
            "LEDGEROPS_OPERATOR_OBJECTS": "/var/lib/ledgerops-objects",
            "LEDGEROPS_OPERATOR_NAMESPACE": state["namespace"],
            "LEDGEROPS_OPERATOR_KEYS_FILE": "/run/ledgerops/provider-keys.json",
        },
    )
    write_secret(volumes / "operator" / "payout-policy.json", policy)
    for name in ("operator", "worker", "backup", "reconciler"):
        write_secret(volumes / name / "provider-keys.json", {"deploy-v1": state["provider_hmac"]})
    profile(
        volumes / "worker",
        {
            "LEDGEROPS_WORKER_DSN": dsn(state, "ledger_dispatcher"),
            "LEDGEROPS_PROVIDER_URL": "http://127.0.0.1:8765",
            "LEDGEROPS_PROVIDER_TOKEN": state["provider_token"],
            "LEDGEROPS_PROVIDER_NAMESPACE": state["namespace"],
            "LEDGEROPS_PROVIDER_KEYS_FILE": "/run/ledgerops/provider-keys.json",
            "LEDGEROPS_RESTORE_MODE": "true",
            "LEDGEROPS_DISPATCH_ENABLED": "false",
            "LEDGEROPS_WORKER_BATCH_SIZE": "10",
            "LEDGEROPS_WORKER_POLL_MS": "500",
            "LEDGEROPS_WORKER_LEASE_SECONDS": "30",
            "LEDGEROPS_PROVIDER_TIMEOUT_MS": "5000",
        },
    )
    profile(
        volumes / "provider",
        {
            "LEDGEROPS_PROVIDER_HMAC_KEY": state["provider_hmac"],
            "LEDGEROPS_PROVIDER_TOKEN": state["provider_token"],
            "LEDGEROPS_PROVIDER_NAMESPACE": state["namespace"],
        },
    )
    profile(volumes / "reconciler", {"LEDGEROPS_RECON_DSN": dsn(state, "ledger_reconciliation")})
    profile(volumes / "bootstrap", {"LEDGEROPS_BOOTSTRAP_STATE_FILE": "/run/bootstrap/state.json"})
    profile(
        volumes / "backup",
        {
            "LEDGEROPS_BACKUP_ADMIN_DSN": dsn(state, "postgres"),
            "LEDGEROPS_PROVIDER_TOKEN": state["provider_token"],
        },
    )
    write_secret(
        volumes / "postgres" / "password",
        state["passwords"]["postgres"].encode(),
        uid=PG_UID,
        raw=True,
    )
    # Write the root-state marker last; interruption requires an explicit inspected
    # recovery of these owned volumes, never an automatic key/password replacement.
    write_secret(state_file, state)
    print('{"event":"owned_secret_volumes_initialized"}')


def state():
    return json.loads(bounded_file(Path(os.environ["LEDGEROPS_BOOTSTRAP_STATE_FILE"])))


def provision():
    current = state()
    with psycopg.connect(dsn(current, "postgres", "postgres"), autocommit=True) as conn:
        for name in GROUPS:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION"
                    ).format(sql.Identifier(name))
                )
        conn.execute("GRANT ledger_worker,ledger_reader,ledger_auth_reader TO ledger_operator")
        for name, groups in LOGINS.items():
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION CONNECTION LIMIT 8 PASSWORD {}"
                    ).format(sql.Identifier(name), sql.Literal(current["passwords"][name]))
                )
            for group in groups:
                conn.execute(
                    sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(name))
                )
            for setting, value in (
                ("statement_timeout", "10s"),
                ("lock_timeout", "5s"),
                ("transaction_timeout", "30s"),
                ("idle_in_transaction_session_timeout", "15s"),
            ):
                conn.execute(
                    sql.SQL("ALTER ROLE {} SET {}={}").format(
                        sql.Identifier(name), sql.Identifier(setting), sql.Literal(value)
                    )
                )
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='ledgerops'").fetchone():
            conn.execute("CREATE DATABASE ledgerops OWNER ledger_migrator")
    with psycopg.connect(dsn(current, "ledger_schema_admin"), autocommit=True) as conn:
        applied = apply_migrations(conn)
    print(json.dumps({"event": "explicit_schema_provisioned", "applied": applied}))


def seed():
    current = state()
    output = Path("/run/bootstrap/acceptance.json")
    if output.exists():
        print('{"event":"synthetic_seed_already_present"}')
        return
    workspace = current["workspace_id"]
    with psycopg.connect(dsn(current, "ledger_schema_admin"), autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE ledger_migrator")
            conn.execute("INSERT INTO ledger.workspace(id,currency) VALUES(%s,'PLN')", (workspace,))
            for account, owner, kind, normal in (
                ("clearing_account", "system_actor", "SYNTHETIC_CLEARING", "D"),
                ("customer_account", "customer_actor", "CUSTOMER_LIABILITY", "C"),
                ("merchant_account", "merchant_actor", "MERCHANT_LIABILITY", "C"),
            ):
                conn.execute(
                    "INSERT INTO ledger.account(workspace_id,id,owner_id,class,normal_side) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (workspace, current[account], current[owner], kind, normal),
                )
                conn.execute(
                    "INSERT INTO ledger.account_balance(workspace_id,account_id,normal_side) "
                    "VALUES(%s,%s,%s)",
                    (workspace, current[account], normal),
                )
            operation, journal = uuid4(), uuid4()
            raw = b"explicit disposable Linux synthetic opening100"
            conn.execute(
                "INSERT INTO ledger.operation(workspace_id,operation_id,kind,payload,"
                "payload_sha256,admission,http_status,response_body) "
                "VALUES(%s,%s,'OPENING',%s,%s,'ACCEPTED',201,%s)",
                (
                    workspace,
                    operation,
                    raw,
                    hashlib.sha256(raw).digest(),
                    Jsonb({"synthetic_opening": True}),
                ),
            )
            conn.execute(
                "INSERT INTO ledger.journal(workspace_id,journal_id,operation_id,kind,"
                "line_count,total_minor) VALUES(%s,%s,%s,'OPENING',2,100)",
                (workspace, journal, operation),
            )
            conn.execute(
                "INSERT INTO ledger.journal_entry(workspace_id,journal_id,line_no,account_id,"
                "side,amount_minor) VALUES(%s,%s,1,%s,'D',100),(%s,%s,2,%s,'C',100)",
                (
                    workspace,
                    journal,
                    current["clearing_account"],
                    workspace,
                    journal,
                    current["customer_account"],
                ),
            )
            conn.execute(
                "UPDATE ledger.account_balance SET debits=100,version=1 WHERE account_id=%s",
                (current["clearing_account"],),
            )
            conn.execute(
                "UPDATE ledger.account_balance SET credits=100,version=1 WHERE account_id=%s",
                (current["customer_account"],),
            )
    tokens = {}
    scopes = {
        "customer": ["purchase:write", "reversal:write", "accounts:read", "operations:read"],
        "merchant": ["payout:write", "payout:read", "accounts:read", "operations:read"],
        "maker": ["approval:propose", "evidence:read"],
        "checker": ["approval:approve", "approval:execute", "evidence:read"],
    }
    with psycopg.connect(dsn(current, "ledger_identity_admin"), autocommit=True) as conn:
        for name, permissions in scopes.items():
            common = {
                "admin_actor_id": current["admin_actor"],
                "reason": "explicit Linux synthetic acceptance",
            }
            auth.set_membership(conn, workspace, current[name + "_actor"], permissions, **common)
            auth.set_client(
                conn, workspace, current[name + "_client"], current[name + "_actor"], **common
            )
            issued = auth.issue_credential(
                conn, workspace, current[name + "_client"], permissions, **common
            )
            tokens[name] = issued.token
    write_secret(
        output,
        {
            key: value
            for key, value in current.items()
            if key not in {"passwords", "provider_hmac", "provider_token"}
        }
        | {"tokens": tokens},
    )
    print('{"event":"explicit_synthetic_acceptance_seeded","opening_minor":"100"}')


def inspect():
    current = state()
    from ledgerops.stream_export import export_stream
    from ledgerops.stream_verification import verify_stream

    provenance = json.loads(Path("/opt/ledgerops-provenance.json").read_bytes())
    with psycopg.connect(dsn(current, "ledger_evidence_reader"), autocommit=True) as conn:
        path = export_stream(
            conn,
            current["workspace_id"],
            "/var/lib/ledgerops-inputs/inspections",
            code_revision=provenance["revision"],
            dirty=provenance["dirty"],
        )
    report = verify_stream(path, scratch_root=os.environ["TMPDIR"])
    if not report["healthy"] or not report["verification_complete"]:
        raise ValueError("COMPLETE_INDEPENDENT_STREAM_VERIFICATION_REQUIRED")
    print(
        json.dumps(
            {
                "event": "independent_linux_inspection",
                "report": report,
                "export_path": str(path),
                "source_format": 10,
            },
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("provision", "seed", "inspect"))
    args = parser.parse_args()
    {"provision": provision, "seed": seed, "inspect": inspect}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"event": "administrator_job_failed", "category": type(exc).__name__}))
        raise SystemExit(2) from None
