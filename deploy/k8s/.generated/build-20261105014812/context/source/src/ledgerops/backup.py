"""Bounded, administrator-only logical backup and restore into a new database.

The manifest is an integrity inventory, not a signature or an independent command
archive. Only restore a backup produced by a trusted administrator: pg_restore
executes the source database's SQL. The dump contains sensitive identity state;
connection passwords are never put in manifest metadata.
"""

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from ledgerops.authentication import invalidate_restored_access
from ledgerops.db.migrations import INDEX_ONLY_MIGRATION
from ledgerops.reconciliation.publication import ObjectStore

MAX_METADATA = 64 * 1024 * 1024
MAX_DUMP = 512 * 1024 * 1024
MAX_OBJECT_TOTAL = 512 * 1024 * 1024
ROLES = (
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
ARCHIVE_TABLES = (
    "operation",
    "request_result",
    "payout",
    "reservation",
    "reservation_event",
    "dispatch_intent",
)
_PG_ENV = frozenset(
    {
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGSSLMODE",
        "PGSSLROOTCERT",
        "PGSSLCERT",
        "PGSSLKEY",
    }
)


class BackupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BackupError("INVALID_BACKUP_JSON") from exc


def _idle(conn):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise BackupError("IDLE_AUTOCOMMIT_REQUIRED")


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir(directory):
    directory.mkdir(mode=0o700)
    _sync(directory.parent)


def _write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    _sync(path.parent)


def _digest_file(path, *, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise BackupError("INVALID_BACKUP_FILE")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > maximum:
                raise BackupError("BACKUP_FILE_TOO_LARGE")
            digest.update(chunk)
        if size != info.st_size:
            raise BackupError("BACKUP_FILE_CHANGED")
    return {"sha256": digest.hexdigest(), "bytes": size}


def _check_ref(ref):
    if (
        type(ref) is not dict
        or ref.keys() != {"sha256", "bytes"}
        or type(ref["sha256"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
        or type(ref["bytes"]) is not int
        or not 0 < ref["bytes"] <= MAX_DUMP
    ):
        raise BackupError("INVALID_OBJECT_REFERENCE")


def _verify(path, ref, *, maximum):
    _check_ref(ref)
    try:
        actual = _digest_file(path, maximum=maximum)
    except OSError as exc:
        raise BackupError("BACKUP_OBJECT_UNREADABLE") from exc
    if actual != ref:
        raise BackupError("BACKUP_OBJECT_MISMATCH")


@dataclass(frozen=True, repr=False)
class PgTools:
    """Explicit trusted executable argv; passwords are environment-only, never logged."""

    environment: dict = field(repr=False)
    dump_command: tuple = ("pg_dump",)
    restore_command: tuple = ("pg_restore",)
    timeout_seconds: int = 120

    def __post_init__(self):
        if (
            type(self.environment) is not dict
            or not self.environment.keys() <= _PG_ENV
            or any(type(value) is not str or "\x00" in value for value in self.environment.values())
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 600
        ):
            raise BackupError("INVALID_PG_TOOLS_CONFIG")
        for command in (self.dump_command, self.restore_command):
            if (
                not isinstance(command, tuple)
                or not command
                or any(type(part) is not str or not part or "\x00" in part for part in command)
            ):
                raise BackupError("INVALID_PG_TOOL_COMMAND")

    def _run(self, command, *, database, source=None, output=None, maximum=MAX_DUMP):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            **self.environment,
            "PGDATABASE": database,
            "PGCONNECT_TIMEOUT": "5",
        }
        deadline = time.monotonic() + self.timeout_seconds
        process = None
        try:
            process = subprocess.Popen(
                command,
                env=env,
                stdin=source or subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                sizes = {"stdout": 0, "stderr": 0}
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackupError("PG_TOOL_TIMEOUT")
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        sizes[key.data] += len(chunk)
                        limit = maximum if key.data == "stdout" else 1024 * 1024
                        if sizes[key.data] > limit:
                            raise BackupError("PG_TOOL_OUTPUT_LIMIT")
                        if key.data == "stdout" and output is not None:
                            output.write(chunk)
                if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                    raise BackupError("PG_TOOL_FAILED")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError("PG_TOOL_FAILED") from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def dump(self, database, snapshot, output):
        self._run(
            (
                *self.dump_command,
                "--format=custom",
                "--schema=ledger",
                "--no-owner",
                "--no-password",
                "--lock-wait-timeout=10000",
                f"--snapshot={snapshot}",
            ),
            database=database,
            output=output,
        )

    def restore(self, database, source):
        self._run(
            (
                *self.restore_command,
                "--no-owner",
                "--exit-on-error",
                "--single-transaction",
                "--no-password",
                "--role=ledger_migrator",
                f"--dbname={database}",
            ),
            database=database,
            source=source,
            maximum=1024 * 1024,
        )


def _archive(conn):
    # PostgreSQL JSON renders bytea as exact hex strings; no Decimal/float money conversion.
    result = {"archive_version": 1, "source": "SAME_DATABASE_SNAPSHOT_NOT_INDEPENDENT"}
    for table in ARCHIVE_TABLES:
        rows = conn.execute(
            sql.SQL(
                "SELECT to_jsonb(t) FROM ledger.{} t "
                'ORDER BY to_jsonb(t)::text COLLATE "C" LIMIT 250001'
            ).format(sql.Identifier(table))
        ).fetchmany(250001)
        if len(rows) > 250000:
            raise BackupError("BUSINESS_ARCHIVE_ROW_LIMIT")
        result[table] = [row[0] for row in rows]
    raw = canonical(result)
    if len(raw) > MAX_METADATA:
        raise BackupError("BUSINESS_ARCHIVE_SIZE_LIMIT")
    return raw


def _object_inventory(conn):
    rows = conn.execute(
        "SELECT workspace_id,sha256,byte_length FROM ledger.recon_object "
        "ORDER BY workspace_id,sha256 LIMIT 250001"
    ).fetchmany(250001)
    if len(rows) > 250000:
        raise BackupError("OBJECT_INVENTORY_LIMIT")
    if sum(row[2] for row in rows) > MAX_OBJECT_TOTAL:
        raise BackupError("OBJECT_TOTAL_SIZE_LIMIT")
    return [{"workspace_id": str(row[0]), "sha256": row[1], "bytes": row[2]} for row in rows]


def create_backup(conn, pg_tools, object_store, destination):
    """Publish dump, request archive and every registered object from one PG snapshot."""
    _idle(conn)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise BackupError("NEW_BACKUP_DESTINATION_REQUIRED")
    stage = destination.parent / f".ledgerops-backup-{uuid4()}"
    _mkdir(stage)
    started = time.monotonic()
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL timezone='UTC'")
            conn.execute("SET LOCAL bytea_output='hex'")
            conn.execute("SET LOCAL statement_timeout='120s'")
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout='180s'")
            snapshot, db_time = conn.execute(
                "SELECT pg_export_snapshot(), clock_timestamp()"
            ).fetchone()
            schema = [
                list(row)
                for row in conn.execute(
                    "SELECT version,sha256 FROM ledger.schema_migration ORDER BY version"
                ).fetchall()
            ]
            inventory = _object_inventory(conn)
            archive = _archive(conn)
            _write(stage / "business-requests.json", archive)
            fd = os.open(stage / "database.dump", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                pg_tools.dump(conn.info.dbname, snapshot, output)
                output.flush()
                os.fsync(output.fileno())
            _mkdir(stage / "objects")
            for item in inventory:
                workspace = stage / "objects" / item["workspace_id"]
                if not workspace.exists():
                    _mkdir(workspace)
                raw = object_store.read(item["workspace_id"], item["sha256"], item["bytes"])
                _write(workspace / item["sha256"], raw)
            manifest = {
                "backup_version": 1,
                "backup_id": str(uuid4()),
                "snapshot_at": db_time.astimezone(UTC).isoformat(),
                "published_at": datetime.now(UTC).isoformat(),
                "server_version_num": int(conn.info.server_version),
                "schema": schema,
                "dump": _digest_file(stage / "database.dump", maximum=MAX_DUMP),
                "business_archive": _digest_file(
                    stage / "business-requests.json", maximum=MAX_METADATA
                ),
                "objects": inventory,
                "namespace_completeness": "NOT_A_PROVIDER_SNAPSHOT",
                "observed_backup_seconds": time.monotonic() - started,
            }
            _write(stage / "manifest.json", canonical(manifest))
        _sync(stage)
        os.rename(stage, destination)
        _sync(destination.parent)
        return manifest
    except BaseException:
        # Only this invocation's randomly named unpublished directory is removable.
        if stage.exists():
            shutil.rmtree(stage)
        raise


def inspect_backup(directory):
    """Check every exact reference before any SQL from the trusted dump is executed."""
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("INVALID_BACKUP_DIRECTORY")
    _digest_file(directory / "manifest.json", maximum=MAX_METADATA)
    manifest = _json((directory / "manifest.json").read_bytes())
    expected = {
        "backup_version",
        "backup_id",
        "snapshot_at",
        "published_at",
        "server_version_num",
        "schema",
        "dump",
        "business_archive",
        "objects",
        "namespace_completeness",
        "observed_backup_seconds",
    }
    if (
        type(manifest) is not dict
        or manifest.keys() != expected
        or type(manifest["backup_version"]) is not int
        or manifest["backup_version"] != 1
    ):
        raise BackupError("UNSUPPORTED_BACKUP_MANIFEST")
    try:
        if str(UUID(manifest["backup_id"])) != manifest["backup_id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise BackupError("INVALID_BACKUP_ID") from exc
    if (
        type(manifest["server_version_num"]) is not int
        or manifest["server_version_num"] // 10000 != 18
        or manifest["namespace_completeness"] != "NOT_A_PROVIDER_SNAPSHOT"
        or type(manifest["observed_backup_seconds"]) not in (int, float)
        or not 0 <= manifest["observed_backup_seconds"] <= 3600
    ):
        raise BackupError("INVALID_BACKUP_METADATA")
    for name in ("snapshot_at", "published_at"):
        try:
            timestamp = datetime.fromisoformat(manifest[name])
            if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise BackupError("INVALID_BACKUP_TIME") from exc
    schema = manifest["schema"]
    if (
        type(schema) is not list
        or not 1 <= len(schema) <= 1000
        or any(
            type(row) is not list
            or len(row) != 2
            or type(row[0]) is not str
            or re.fullmatch(r"[0-9]{3}_[a-z_]+\.sql", row[0]) is None
            or type(row[1]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", row[1]) is None
            for row in schema
        )
        or schema != sorted(schema)
        or len({row[0] for row in schema}) != len(schema)
    ):
        raise BackupError("INVALID_SCHEMA_INVENTORY")
    _verify(directory / "database.dump", manifest["dump"], maximum=MAX_DUMP)
    _verify(
        directory / "business-requests.json",
        manifest["business_archive"],
        maximum=MAX_METADATA,
    )
    objects, seen = manifest["objects"], set()
    total_object_bytes = 0
    if type(objects) is not list or len(objects) > 250000:
        raise BackupError("INVALID_OBJECT_INVENTORY")
    for item in objects:
        if type(item) is not dict or item.keys() != {"workspace_id", "sha256", "bytes"}:
            raise BackupError("INVALID_OBJECT_INVENTORY")
        try:
            if str(UUID(item["workspace_id"])) != item["workspace_id"]:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise BackupError("INVALID_OBJECT_WORKSPACE") from exc
        _check_ref({k: item[k] for k in ("sha256", "bytes")})
        total_object_bytes += item["bytes"]
        if total_object_bytes > MAX_OBJECT_TOTAL:
            raise BackupError("OBJECT_TOTAL_SIZE_LIMIT")
        identity = (item["workspace_id"], item["sha256"])
        if identity in seen:
            raise BackupError("DUPLICATE_OBJECT_INVENTORY")
        seen.add(identity)
        parent = directory / "objects" / item["workspace_id"]
        if parent.is_symlink() or (directory / "objects").is_symlink():
            raise BackupError("SYMLINK_OBJECT_DIRECTORY")
        _verify(
            parent / item["sha256"],
            {k: item[k] for k in ("sha256", "bytes")},
            maximum=64 * 1024 * 1024,
        )
    return manifest


def restore_backup(
    admin_conn,
    pg_tools,
    backup,
    *,
    new_database,
    destination_objects,
    admin_actor_id,
    reason,
    restore_id,
):
    """Restore only a new database; an audited generation gate is mandatory before success.

    Restored credentials are revoked and approval generations invalidated using
    the existing audited auth administrator protocol before this returns.
    This function never starts a service, worker, or outbound payment request.
    """
    _idle(admin_conn)
    expected_roles = ROLES
    if (
        type(new_database) is not str
        or not re.fullmatch(r"ledgerops_restore_[a-z0-9_]{8,40}", new_database)
        or type(admin_actor_id) is not UUID
        or type(reason) is not str
        or not 1 <= len(reason.encode()) <= 256
        or type(restore_id) is not UUID
    ):
        raise BackupError("INVALID_RESTORE_TARGET")
    manifest = inspect_backup(backup)
    destination = Path(destination_objects).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("NEW_OBJECT_DESTINATION_REQUIRED")
    roles = admin_conn.execute(
        "SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolcanlogin "
        "FROM pg_roles WHERE rolname=ANY(%s)",
        (list(expected_roles),),
    ).fetchall()
    if {row[0] for row in roles} != set(expected_roles) or any(
        any(row[1:6])
        or (
            row[0]
            in {
                "ledger_migrator",
                "ledger_auth_reader",
                "ledger_auth_admin",
                "ledger_operator",
            }
            and row[6]
        )
        for row in roles
    ):
        raise BackupError("RESTORE_ROLE_PROVISIONING_REQUIRED")
    for role in expected_roles:
        if (
            role != "ledger_migrator"
            and admin_conn.execute(
                "SELECT pg_has_role(%s,'ledger_migrator','MEMBER')", (role,)
            ).fetchone()[0]
        ):
            raise BackupError("RUNTIME_HAS_MIGRATOR_MEMBERSHIP")
    unsafe = admin_conn.execute(
        "SELECT 1 FROM pg_roles child CROSS JOIN pg_roles parent WHERE child.rolname=ANY(%s) "
        "AND parent.oid<>child.oid AND pg_has_role(child.oid,parent.oid,'MEMBER') AND "
        "((parent.rolname=ANY(%s) AND NOT (child.rolname='ledger_operator' AND "
        "parent.rolname IN ('ledger_worker','ledger_reader','ledger_auth_reader'))) "
        "OR parent.rolsuper OR parent.rolcreaterole OR "
        "parent.rolcreatedb OR parent.rolreplication OR parent.rolbypassrls OR "
        "parent.rolname IN ('pg_read_all_data','pg_write_all_data','pg_read_server_files',"
        "'pg_write_server_files','pg_execute_server_program')) LIMIT 1",
        (list(expected_roles), list(expected_roles)),
    ).fetchone()
    if unsafe:
        raise BackupError("RESTORE_ROLE_MEMBERSHIP_UNSAFE")
    for parent in ("ledger_worker", "ledger_reader", "ledger_auth_reader"):
        if not admin_conn.execute(
            "SELECT pg_has_role('ledger_operator',%s,'MEMBER')", (parent,)
        ).fetchone()[0]:
            raise BackupError("RESTORE_OPERATOR_ROLE_PROVISIONING_REQUIRED")
    if admin_conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (new_database,)).fetchone():
        raise BackupError("RESTORE_DATABASE_ALREADY_EXISTS")
    created = False
    started = time.monotonic()
    try:
        admin_conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER ledger_migrator ALLOW_CONNECTIONS false").format(
                sql.Identifier(new_database)
            )
        )
        created = True
        admin_conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(new_database))
        )
        admin_conn.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(new_database))
        )
        # Runtime roles cannot connect until restore validation and generation invalidation succeed.
        with (Path(backup) / "database.dump").open("rb") as source:
            pg_tools.restore(new_database, source)
        params = admin_conn.info.get_parameters()
        params.update(dbname=new_database, password=admin_conn.info.password)
        with psycopg.connect(**params, autocommit=True) as restored:
            restored.execute("SET timezone='UTC'")
            restored.execute("SET bytea_output='hex'")
            schema = [
                list(row)
                for row in restored.execute(
                    "SELECT version,sha256 FROM ledger.schema_migration ORDER BY version"
                ).fetchall()
            ]
            if schema != manifest["schema"]:
                raise BackupError("RESTORED_SCHEMA_MISMATCH")
            if _object_inventory(restored) != manifest["objects"]:
                raise BackupError("RESTORED_OBJECT_INVENTORY_MISMATCH")
            archive = _archive(restored)
            if {
                "sha256": hashlib.sha256(archive).hexdigest(),
                "bytes": len(archive),
            } != manifest["business_archive"]:
                raise BackupError("RESTORED_BUSINESS_ARCHIVE_MISMATCH")
            installed = [
                [path.name, hashlib.sha256(path.read_bytes()).hexdigest()]
                for path in sorted(
                    files("ledgerops.db").joinpath("sql").iterdir(),
                    key=lambda path: path.name,
                )
                if path.name.endswith(".sql")
            ]
            # Verify without calling migrations: that entry point grants runtime CONNECT.
            # 011 changes only a physical access path. An exact 001–010 backup
            # remains restorable with CONNECT closed; it is not auto-upgraded
            # through apply_migrations(), which also grants runtime CONNECT.
            compatible_without_index = (
                len(installed) == 11
                and installed[-1][0] == INDEX_ONLY_MIGRATION
                and len(schema) == 10
                and schema == installed[:-1]
            )
            if schema != installed and not compatible_without_index:
                raise BackupError("RESTORE_REQUIRES_EXACT_INSTALLED_SCHEMA")
            _closed_runtime(restored)
            generation = invalidate_restored_access(
                restored,
                backup_id=manifest["backup_id"],
                restore_id=str(restore_id),
                admin_actor_id=str(admin_actor_id),
                reason=reason,
            )
            _closed_runtime(restored)
            store = ObjectStore(destination)
            for item in manifest["objects"]:
                raw = (
                    Path(backup) / "objects" / item["workspace_id"] / item["sha256"]
                ).read_bytes()
                temporary, digest, size = store._stage(item["workspace_id"], raw)
                if (digest, size) != (item["sha256"], item["bytes"]):
                    raise BackupError("BACKUP_OBJECT_CHANGED")
                store._promote(item["workspace_id"], temporary, item["sha256"], item["bytes"])
            # Leave runtime CONNECT revoked; opening restored traffic is a later action.
        return {
            "restore_id": str(restore_id),
            "backup_id": manifest["backup_id"],
            "database": new_database,
            "generation": generation,
            "external_dispatch_enabled": False,
            "runtime_connect_enabled": False,
            "observed_restore_seconds": time.monotonic() - started,
            "backup_snapshot_at": manifest["snapshot_at"],
        }
    except BaseException:
        if created:
            admin_conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(new_database))
            )
        # An interrupted object copy has no live DB references and is retained for inspection.
        raise


def _closed_runtime(conn):
    """All non-owner, non-superuser identities with schema access must lack CONNECT.

    Runtime logins inheriting one of the schema groups are included, even when the
    administrator used a deployment-specific login name. Dedicated administrator
    logins inheriting the schema-owner role and superusers are trusted boundaries.
    Canonical runtime groups must never inherit that owner role.
    """
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE NOT rolsuper "
        "AND NOT pg_has_role(oid,'ledger_migrator','MEMBER') "
        "AND has_schema_privilege(oid,'ledger','USAGE') "
        "AND has_database_privilege(oid,current_database(),'CONNECT')"
    ).fetchall()
    if rows:
        raise BackupError("RUNTIME_CONNECT_MUST_REMAIN_CLOSED")
    active = conn.execute(
        "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
        "AND pid<>pg_backend_pid() AND usename<>current_user "
        "AND NOT pg_has_role(usename,'ledger_migrator','MEMBER') LIMIT 1"
    ).fetchone()
    if active:
        raise BackupError("RESTORE_REQUIRES_NO_RUNTIME_SESSIONS")
