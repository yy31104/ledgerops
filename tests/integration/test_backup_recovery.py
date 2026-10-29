import http.client
import json
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from ledgerops import authentication as auth
from ledgerops.backup import (
    BackupError,
    PgTools,
    canonical,
    create_backup,
    inspect_backup,
    restore_backup,
)
from ledgerops.domain import ProviderNamespaceId
from ledgerops.export import capture_snapshot
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.provider_protocol import ProviderTrust
from ledgerops.reconciliation.publication import ObjectStore, register_object
from ledgerops.recovery import capture_history, reconcile_restored_history
from ledgerops.verification import verify_snapshot
from tests.conftest import POSTGRES_IMAGE, docker
from tests.integration.payout_helpers import (
    NAMESPACE,
    admit,
    fund_merchant,
    payout_input,
)
from tests.integration.purchase_helpers import purchase_input
from tests.integration.test_authentication import ADMIN, provision
from tests.provider.test_process import SECRET, TOKEN, ProcessProvider

pytestmark = pytest.mark.integration
TRUST = ProviderTrust(ProviderNamespaceId.parse(NAMESPACE), {"test-v1": SECRET})
MEASUREMENTS = []


@pytest.fixture
def provider(tmp_path):
    root = tmp_path / "provider"
    root.mkdir()
    provider = ProcessProvider(root, namespace=NAMESPACE).start()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward()

        def do_PUT(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path))
            conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=10)
            try:
                conn.request(
                    self.command,
                    self.path,
                    body=raw,
                    headers={
                        "Authorization": self.headers.get("Authorization", ""),
                        "Content-Type": "application/json",
                    },
                )
                response = conn.getresponse()
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                conn.close()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    provider.ready["url"] = f"http://127.0.0.1:{server.server_port}"
    provider.requests = requests
    try:
        yield provider
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        provider.stop()


@pytest.fixture
def pg_tools(db):
    # Use the same owned pinned-server container for client tools. This avoids a
    # native pg_dump dependency and works with Docker contexts on macOS/Linux.
    endpoint = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    selected = None
    for container in docker(
        "ps", "--filter", "label=ledgerops.scope=disposable-test", "--format", "{{.ID}}"
    ).split():
        ports = json.loads(
            docker(
                "inspect",
                "--format",
                '{{json (index .NetworkSettings.Ports "5432/tcp")}}',
                container,
            )
        )
        if any(int(binding["HostPort"]) == db.port for binding in ports or []):
            assert docker("inspect", "--format", "{{.Config.Image}}", container) == POSTGRES_IMAGE
            assert selected is None
            selected = container
    assert selected is not None, "owned disposable PostgreSQL container not found"
    command = (
        "docker",
        "--host",
        endpoint,
        "exec",
        "-i",
        "--env",
        "PGHOST",
        "--env",
        "PGPORT",
        "--env",
        "PGUSER",
        "--env",
        "PGPASSWORD",
        "--env",
        "PGDATABASE",
        selected,
    )
    return PgTools(
        {
            "PGHOST": "127.0.0.1",
            "PGPORT": "5432",
            "PGUSER": "postgres",
            "PGPASSWORD": db.admin_password,
        },
        dump_command=(*command, "pg_dump"),
        restore_command=(*command, "pg_restore"),
    )


def source_backup(db, pg_tools, root, *, store=None):
    store = store or ObjectStore(root / "source-objects")
    with db.connect("admin") as conn:
        conn.execute("SET timezone='Europe/Warsaw'")
        conn.execute("SET bytea_output='escape'")
        manifest = create_backup(conn, pg_tools, store, root / "backup")
        assert conn.execute("SHOW timezone").fetchone() == ("Europe/Warsaw",)
    assert inspect_backup(root / "backup") == manifest
    return manifest


def restore(db, pg_tools, root):
    name = f"ledgerops_restore_{uuid4().hex}"
    with db.connect("admin") as conn:
        report = restore_backup(
            conn,
            pg_tools,
            root / "backup",
            new_database=name,
            destination_objects=root / "restored-objects",
            restore_id=uuid4(),
            admin_actor_id=uuid4(),
            reason="local restore drill",
        )
    return replace(db, name=name), report


def drop(db):
    with replace(db, name="postgres").connect("admin") as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db.name)))


def history(provider, *, limit=1):
    client = ProviderHTTPClient(provider.ready["url"], TOKEN)
    return client, capture_history(
        lambda **kw: client.history(**kw).body, trust=TRUST, page_limit=limit
    )


def provider_pay(provider, db):
    with db.connect("reader") as conn:
        key, raw = conn.execute(
            "SELECT provider_key,provider_request FROM ledger.payout"
        ).fetchone()
    client = ProviderHTTPClient(provider.ready["url"], TOKEN)
    assert client.put(key, raw).http_status == 200
    return key, raw


@pytest.mark.parametrize("open_freeze", [False, True])
def test_actual_dump_restore_provider_ahead_only_history_lookup_and_revoked_tokens(
    seeded_db, golden, pg_tools, provider, tmp_path, open_freeze
):
    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    issued = provision(seeded_db, purchase_input(golden)[0])
    objects = ObjectStore(tmp_path / "source-objects")
    with seeded_db.connect("reconciler") as conn:
        ref = register_object(
            conn,
            golden["workspace"]["id"],
            objects,
            b'{"synthetic":"retained"}',
            publisher_id=uuid4(),
        )
    freeze = None
    if open_freeze:
        from ledgerops.db.maintenance import freeze_workspace

        freeze = str(uuid4())
        with seeded_db.connect("maintenance") as conn:
            freeze_workspace(conn, golden["workspace"]["id"], freeze, ADMIN, "retained open freeze")
    manifest = source_backup(seeded_db, pg_tools, tmp_path, store=objects)
    # Token revocation and provider effect occur after the backup boundary.
    with seeded_db.connect("auth_admin") as conn:
        auth.revoke_credential(
            conn,
            golden["workspace"]["id"],
            str(issued.credential_id),
            admin_actor_id=ADMIN,
            reason="after backup revoke",
        )
    provider_pay(provider, seeded_db)
    before = list(provider.requests)
    restored, report = restore(seeded_db, pg_tools, tmp_path)
    try:
        assert report["generation"]["workspaces"][0]["revoked_count"] == 1
        assert report["generation"]["workspaces"][0]["generation"] != str(issued.generation)
        for role in ("app", "reader", "worker", "service", "auth_admin", "reconciler"):
            with pytest.raises(psycopg.OperationalError):
                restored.connect(role)
        with restored.connect("admin") as conn:
            with pytest.raises(auth.AuthenticationRequired):
                auth.authenticate(conn, issued.token)
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
        client, snapshot = history(provider)
        with restored.connect("admin") as conn:
            recovered = reconcile_restored_history(
                conn, snapshot, trust=TRUST, lookup=lambda k: client.lookup(k).body
            )
            assert recovered["status"] == "RECOVERED"
            assert conn.execute(
                "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
                (golden["purchase_request"]["merchant_account_id"],),
            ).fetchone() == (18, 0)
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (1,)
            if freeze:
                assert conn.execute(
                    "SELECT current_freeze_id::text FROM ledger.workspace"
                ).fetchone() == (freeze,)
                assert conn.execute(
                    "SELECT resumed_at FROM ledger.maintenance_freeze"
                ).fetchone() == (None,)
            independent = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
            assert (
                independent.structure_ok
                and independent.projection_ok
                and independent.request_intent_ok
            )
            assert conn.execute(
                "SELECT DISTINCT channel FROM ledger.observation_receipt ORDER BY channel"
            ).fetchall() == [("HISTORY",), ("LOOKUP",)]
            assert conn.execute(
                "SELECT http_status FROM ledger.operation WHERE operation_id=%s",
                (str(payout_input(golden)[1].operation_id),),
            ).fetchone() == (202,)
        assert (
            ObjectStore(tmp_path / "restored-objects").read(
                golden["workspace"]["id"], ref.sha256, ref.bytes
            )
            == b'{"synthetic":"retained"}'
        )
        after = list(provider.requests)
        assert [row for row in before if row[0] == "PUT"] == [
            row for row in after if row[0] == "PUT"
        ]
        assert all(row[0] == "GET" for row in after[len(before) :])
        with sqlite3.connect(provider.path) as sqlconn:
            assert sqlconn.execute("SELECT count(*) FROM effects").fetchone() == (1,)
        MEASUREMENTS.append(
            {
                "drill": "admission_retained_provider_ahead" + ("_frozen" if freeze else ""),
                "observed_snapshot_age_seconds": (
                    datetime.now(UTC) - datetime.fromisoformat(manifest["snapshot_at"])
                ).total_seconds(),
                "rpo_missing_admissions": 0,
                "rpo_missing_provider_effects_after_recovery": 0,
                "backup": manifest,
                "restore": report,
                "recovery": recovered,
            }
        )
    finally:
        drop(restored)


def test_before_admission_backup_paid_provider_is_restore_gap_no_guessed_key(
    seeded_db, golden, pg_tools, provider, tmp_path
):
    fund_merchant(seeded_db, golden)
    manifest = source_backup(seeded_db, pg_tools, tmp_path)
    assert admit(seeded_db, golden).http_status == 202
    provider_pay(provider, seeded_db)
    restored, report = restore(seeded_db, pg_tools, tmp_path)
    try:
        _client, snapshot = history(provider)
        with restored.connect("admin") as conn:
            recovered = reconcile_restored_history(
                conn,
                snapshot,
                trust=TRUST,
                lookup=lambda _: pytest.fail("gap must not LOOKUP an invented key"),
            )
            assert recovered["status"] == "RESTORE_GAP"
            assert recovered["gaps"][0]["code"] == "MISSING_ADMISSION_KEY_AND_202"
            assert conn.execute("SELECT count(*) FROM ledger.payout").fetchone() == (0,)
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute(
                "SELECT count(*) FROM ledger.operation WHERE kind='PAYOUT'"
            ).fetchone() == (0,)
        MEASUREMENTS.append(
            {
                "drill": "backup_before_admission_missing_independent_archive",
                "observed_snapshot_age_seconds": (
                    datetime.now(UTC) - datetime.fromisoformat(manifest["snapshot_at"])
                ).total_seconds(),
                "rpo_missing_admissions": 1,
                "recovered": False,
                "backup": manifest,
                "restore": report,
                "recovery": recovered,
            }
        )
    finally:
        drop(restored)


def test_dump_and_business_archive_share_exported_rr_snapshot(
    seeded_db, golden, pg_tools, tmp_path
):
    fund_merchant(seeded_db, golden)

    class ConcurrentTools:
        def dump(self, database, snapshot, output):
            assert admit(seeded_db, golden).http_status == 202
            return pg_tools.dump(database, snapshot, output)

        restore = pg_tools.restore

    source_backup(seeded_db, ConcurrentTools(), tmp_path)
    restored, _ = restore(seeded_db, pg_tools, tmp_path)
    try:
        with restored.connect("admin") as conn:
            assert conn.execute("SELECT count(*) FROM ledger.payout").fetchone() == (0,)
        archive = json.loads((tmp_path / "backup" / "business-requests.json").read_bytes())
        assert archive["payout"] == []
        assert archive["source"] == "SAME_DATABASE_SNAPSHOT_NOT_INDEPENDENT"
    finally:
        drop(restored)


def test_backup_open_freeze_audit_is_preserved_on_restore(seeded_db, golden, pg_tools, tmp_path):
    from ledgerops.db.maintenance import freeze_workspace

    freeze_id = str(uuid4())
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, golden["workspace"]["id"], freeze_id, ADMIN, "open freeze")
    source_backup(seeded_db, pg_tools, tmp_path)
    restored, report = restore(seeded_db, pg_tools, tmp_path)
    try:
        with restored.connect("admin") as conn:
            assert conn.execute(
                "SELECT mode,current_freeze_id::text FROM ledger.workspace"
            ).fetchone() == ("RESTORE", freeze_id)
            assert conn.execute("SELECT resumed_at FROM ledger.maintenance_freeze").fetchone() == (
                None,
            )
        assert report["generation"]["workspaces"][0]["previous_mode"] == "FROZEN"
        assert report["generation"]["workspaces"][0]["previous_freeze_id"] == freeze_id
    finally:
        drop(restored)


@pytest.mark.parametrize(
    "fault",
    [
        "dump",
        "business-requests",
        "object",
        "missing_dump",
        "missing_object",
        "symlink_object",
        "schema",
        "role",
        "inherited_role",
        "existing",
        "tool",
    ],
)
def test_corruption_role_and_tool_failures_never_open_or_replace_database(
    seeded_db, golden, pg_tools, tmp_path, fault
):
    store = ObjectStore(tmp_path / "source-objects")
    with seeded_db.connect("reconciler") as conn:
        ref = register_object(
            conn,
            golden["workspace"]["id"],
            store,
            b"immutable object",
            publisher_id=uuid4(),
        )
    source_backup(seeded_db, pg_tools, tmp_path, store=store)
    name = f"ledgerops_restore_{uuid4().hex}"
    with seeded_db.connect("admin") as conn:
        if fault in {"dump", "business-requests", "object"}:
            path = (
                tmp_path / "backup" / "objects" / golden["workspace"]["id"] / ref.sha256
                if fault == "object"
                else tmp_path
                / "backup"
                / ("database.dump" if fault == "dump" else "business-requests.json")
            )
            path.write_bytes(path.read_bytes() + b"corrupt")
        if fault in {"missing_dump", "missing_object", "symlink_object"}:
            path = (
                tmp_path / "backup" / "database.dump"
                if fault == "missing_dump"
                else tmp_path / "backup" / "objects" / golden["workspace"]["id"] / ref.sha256
            )
            if fault == "symlink_object":
                original = path.with_suffix(".original")
                path.rename(original)
                path.symlink_to(original)
            else:
                path.unlink()
        if fault == "schema":
            path = tmp_path / "backup" / "manifest.json"
            manifest = json.loads(path.read_bytes())
            manifest["schema"][0][1] = "0" * 64
            path.write_bytes(canonical(manifest))
        if fault == "role":
            conn.execute("ALTER ROLE ledger_operator CREATEROLE")
        if fault == "inherited_role":
            conn.execute("GRANT pg_read_all_data TO ledger_reader")
        if fault == "existing":
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER ledger_migrator").format(sql.Identifier(name))
            )
        tools = replace(pg_tools, restore_command=("false",)) if fault == "tool" else pg_tools
        try:
            with pytest.raises(BackupError):
                restore_backup(
                    conn,
                    tools,
                    tmp_path / "backup",
                    new_database=name,
                    destination_objects=tmp_path / "restored-objects",
                    restore_id=uuid4(),
                    admin_actor_id=uuid4(),
                    reason="failure drill",
                )
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            assert bool(exists) == (fault == "existing")
        finally:
            if fault == "role":
                conn.execute("ALTER ROLE ledger_operator NOCREATEROLE")
            if fault == "inherited_role":
                conn.execute("REVOKE pg_read_all_data FROM ledger_reader")
            if fault == "existing":
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="module", autouse=True)
def persist_measurements(tmp_path_factory):
    yield
    if MEASUREMENTS:
        target = Path(
            os.environ.get("LEDGEROPS_RECOVERY_MEASUREMENTS")
            or tmp_path_factory.getbasetemp() / "recovery-measurements.json"
        )
        target.write_text(json.dumps(MEASUREMENTS, indent=2))


def test_actual_service_worker_defaults_keep_restored_normal_unwritable(
    seeded_db, golden, pg_tools, provider, tmp_path, monkeypatch
):
    import subprocess

    from tests.integration.http_helpers import ProcessService
    from tests.integration.test_http_service import payload
    from tests.integration.test_payout_worker_process import ProcessWorker

    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    source_backup(seeded_db, pg_tools, tmp_path)
    restored, _ = restore(seeded_db, pg_tools, tmp_path)
    original = subprocess.Popen

    def defaults(*args, **kwargs):
        environment = dict(kwargs["env"])
        for key in (
            "LEDGEROPS_RESTORE_MODE",
            "LEDGEROPS_API_WRITES_ENABLED",
            "LEDGEROPS_DISPATCH_ENABLED",
        ):
            environment.pop(key, None)
        kwargs["env"] = environment
        return original(*args, **kwargs)

    try:
        with restored.connect("admin") as conn:
            # Simulate the stale NORMAL value explicitly; runtime CONNECT stays closed.
            conn.execute("UPDATE ledger.workspace SET mode='NORMAL'")
            before = conn.execute("SELECT count(*) FROM ledger.operation").fetchone()
        requests = list(provider.requests)
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", defaults)
            service = ProcessService(restored, tmp_path / "default-service")
            worker = ProcessWorker(restored, tmp_path / "default-worker", provider.ready["url"])
        try:
            assert service.request("GET", "/health/live")[0] == 200
            assert service.request("GET", "/health/ready")[0] == 503
            status, raw, _ = service.request(
                "POST",
                "/v1/purchases",
                payload(golden),
                token="lo1_" + "x" * 43,
                headers={"Idempotency-Key": "restore-default"},
            )
            assert (
                status == 503
                and json.loads(raw)["error"]["code"] == "PHYSICAL_RESTORE_OR_WRITES_DISABLED"
            )
            events = worker.wait(2)
            assert events == [
                {
                    "event": "configuration_error",
                    "reason": "RESTORE_MODE_DISPATCH_DISABLED",
                }
            ]
            assert provider.requests == requests
            with restored.connect("admin") as conn:
                assert conn.execute("SELECT count(*) FROM ledger.operation").fetchone() == before
                assert conn.execute("SELECT count(*) FROM ledger.transport_attempt").fetchone() == (
                    0,
                )
        finally:
            worker.close()
            service.close()
    finally:
        drop(restored)


def test_invalid_history_and_recovery_exception_leave_closed_restore_and_release_locks(
    seeded_db, golden, pg_tools, provider, tmp_path, monkeypatch
):
    from ledgerops import recovery

    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    source_backup(seeded_db, pg_tools, tmp_path)
    provider_pay(provider, seeded_db)
    restored, _ = restore(seeded_db, pg_tools, tmp_path)
    try:
        client, snapshot = history(provider)
        with restored.connect("admin") as conn:
            bad = replace(snapshot, events=())
            with pytest.raises(ValueError, match="snapshot mismatch"):
                reconcile_restored_history(
                    conn,
                    bad,
                    trust=TRUST,
                    lookup=lambda _: pytest.fail("invalid history"),
                )

            def fail(*args, **kwargs):
                raise RuntimeError("injected resolver failure")

            monkeypatch.setattr(recovery, "receive_observation", fail)
            with pytest.raises(RuntimeError, match="resolver failure"):
                reconcile_restored_history(
                    conn,
                    snapshot,
                    trust=TRUST,
                    lookup=lambda key: client.lookup(key).body,
                )
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute(
                "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
            ).fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
            assert conn.execute(
                "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
                (golden["purchase_request"]["merchant_account_id"],),
            ).fetchone() == (80, 62)
    finally:
        drop(restored)


def test_restored_payout_without_original_request_key_and_202_is_gap(
    seeded_db, golden, pg_tools, provider, tmp_path
):
    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    provider_pay(provider, seeded_db)
    # Trusted-owner source corruption: retain instruction but remove its client-key receipt.
    with seeded_db.connect("admin") as conn:
        conn.execute("ALTER TABLE ledger.request_result DISABLE TRIGGER USER")
        try:
            conn.execute("DELETE FROM ledger.request_result WHERE route_family='payout:v1'")
        finally:
            conn.execute("ALTER TABLE ledger.request_result ENABLE TRIGGER USER")
    source_backup(seeded_db, pg_tools, tmp_path)
    restored, _ = restore(seeded_db, pg_tools, tmp_path)
    try:
        _client, snapshot = history(provider)
        with restored.connect("admin") as conn:
            result = reconcile_restored_history(
                conn,
                snapshot,
                trust=TRUST,
                lookup=lambda _: pytest.fail("missing key cannot be invented"),
            )
            assert result["status"] == "RESTORE_GAP"
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (0,)
    finally:
        drop(restored)


def test_actual_restore_invalidates_pending_approved_action_after_fresh_bootstrap(
    seeded_db, golden, pg_tools, tmp_path
):
    from tests.integration.test_operator_actions import (
        SCOPES,
        approve,
        execute,
        operator_story,
        propose,
    )

    story = operator_story(seeded_db, golden, tmp_path / "operator-story")
    proposal = propose(story)
    assert approve(story, proposal).http_status == 201
    source_backup(seeded_db, pg_tools, tmp_path, store=story["store"])
    restored, report = restore(seeded_db, pg_tools, tmp_path)
    try:
        assert report["generation"]["workspaces"][0]["revoked_count"] == 2
        with restored.connect("admin") as conn:
            assert conn.execute("SELECT count(*) FROM ledger.operator_approval").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM ledger.operator_execution").fetchone() == (0,)
            fresh = auth.issue_credential(
                conn,
                str(story["workspace"]),
                str(story["checker"].principal.client_id),
                SCOPES,
                admin_actor_id=ADMIN,
                reason="fresh bootstrap after physical restore",
            )
            checker = auth.authenticate(conn, fresh.token)
            assert checker.principal == story["checker"].principal
            assert checker.generation != story["checker"].generation
            # Exercise execution as trusted admin while runtime CONNECT stays closed.
            conn.execute("UPDATE ledger.workspace SET mode='NORMAL'")
            try:
                restored_story = {
                    **story,
                    "database": restored,
                    "store": ObjectStore(tmp_path / "restored-objects"),
                }
                result = execute(restored_story, proposal, credential=checker, conn=conn)
                assert (
                    result.http_status == 409 and result.body["code"] == "STALE_RESTORE_GENERATION"
                )
                assert conn.execute(
                    "SELECT count(*) FROM ledger.operator_execution"
                ).fetchone() == (0,)
                assert conn.execute("SELECT count(*) FROM ledger.payout_resolution").fetchone() == (
                    0,
                )
                assert conn.execute(
                    "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
                    (golden["purchase_request"]["merchant_account_id"],),
                ).fetchone() == (80, 62)
            finally:
                conn.execute("UPDATE ledger.workspace SET mode='RESTORE'")
    finally:
        drop(restored)


def test_actual_backup_cli_publishes_restores_and_retains_history(
    seeded_db, golden, pg_tools, provider, tmp_path
):
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    tools = tmp_path / "native-tools"
    tools.mkdir()
    prefix = pg_tools.dump_command[:-1]
    for name in ("pg_dump", "pg_restore"):
        executable = tools / name
        executable.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            f"prefix={prefix!r}\n"
            "environment=dict(os.environ, PGHOST='127.0.0.1', PGPORT='5432')\n"
            "os.execvpe(prefix[0], [*prefix, os.path.basename(sys.argv[0]), "
            "*sys.argv[1:]], environment)\n"
        )
        executable.chmod(0o700)
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
    }

    def admin_dsn(database):
        return make_conninfo(
            host="127.0.0.1",
            port=database.port,
            dbname=database.name,
            user="postgres",
            password=database.admin_password,
        )

    def run(*args):
        result = subprocess.run(
            [sys.executable, "-m", "ledgerops.backup_cli", *map(str, args)],
            env=environment,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert seeded_db.admin_password.encode() not in result.stdout + result.stderr
        return json.loads(result.stdout)

    environment["LEDGEROPS_BACKUP_ADMIN_DSN"] = admin_dsn(seeded_db)
    objects = tmp_path / "empty-objects"
    objects.mkdir()
    backup = tmp_path / "cli-backup"
    manifest = run("backup", backup, "--objects", objects)
    assert run("inspect", backup) == manifest
    provider_pay(provider, seeded_db)
    name = f"ledgerops_restore_{uuid4().hex}"
    restored = replace(seeded_db, name=name)
    report = run(
        "restore",
        backup,
        "--database",
        name,
        "--objects",
        tmp_path / "cli-restored-objects",
        "--restore-id",
        uuid4(),
        "--admin-actor",
        uuid4(),
        "--reason",
        "actual CLI drill",
    )
    try:
        assert report["runtime_connect_enabled"] is False
        environment["LEDGEROPS_BACKUP_ADMIN_DSN"] = admin_dsn(restored)
        environment["LEDGEROPS_PROVIDER_TOKEN"] = TOKEN
        keys = tmp_path / "keys.json"
        keys.write_text(json.dumps({"test-v1": SECRET.hex()}))
        keys.chmod(0o600)
        path = tmp_path / "cli-history.json"
        result = run(
            "history",
            "--provider-url",
            provider.ready["url"],
            "--namespace",
            NAMESPACE,
            "--keys-file",
            keys,
            "--history-file",
            path,
        )
        assert (
            result["status"] == "RECOVERED"
            and result["retained_history"]["bytes"] == path.stat().st_size
        )
        from ledgerops.recovery import load_history

        assert load_history(path, trust=TRUST).watermark == 1
        with restored.connect("admin") as conn:
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute(
                "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s",
                (golden["purchase_request"]["merchant_account_id"],),
            ).fetchone() == (18, 0)
    finally:
        drop(restored)


def test_new_database_rejects_preconnect_until_acl_and_trusted_schema_admin_remains_allowed(
    seeded_db, pg_tools, tmp_path
):
    import secrets

    source_backup(seeded_db, pg_tools, tmp_path)
    name = f"ledgerops_restore_{uuid4().hex}"
    role = "ledger_restore_schema_admin"
    password = secrets.token_urlsafe(24)
    observed = []
    with seeded_db.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        admin.execute(sql.SQL("GRANT ledger_migrator TO {}").format(sql.Identifier(role)))

        class Guarded:
            def __getattr__(self, field):
                return getattr(admin, field)

            def execute(self, query, params=None):
                rendered = query if type(query) is str else query.as_string(admin)
                if rendered.startswith("REVOKE ALL ON DATABASE"):
                    assert admin.execute(
                        "SELECT datallowconn FROM pg_database WHERE datname=%s", (name,)
                    ).fetchone() == (False,)
                    with pytest.raises(psycopg.OperationalError):
                        replace(seeded_db, name=name).connect("app")
                    observed.append("closed-before-acl")
                return admin.execute(query, params)

        try:
            report = restore_backup(
                Guarded(),
                pg_tools,
                tmp_path / "backup",
                new_database=name,
                destination_objects=tmp_path / "restored-objects",
                restore_id=uuid4(),
                admin_actor_id=uuid4(),
                reason="preconnect and trusted owner boundary",
            )
            assert observed == ["closed-before-acl"] and report["runtime_connect_enabled"] is False
            with psycopg.connect(
                host="127.0.0.1",
                port=seeded_db.port,
                dbname=name,
                user=role,
                password=password,
                autocommit=True,
            ) as schema_admin:
                assert schema_admin.execute("SELECT mode FROM ledger.workspace").fetchone() == (
                    "RESTORE",
                )
            with pytest.raises(psycopg.OperationalError):
                replace(seeded_db, name=name).connect("service")
        finally:
            if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_parent_fsync_failure_retains_complete_backup_for_inspection(
    seeded_db, pg_tools, tmp_path, monkeypatch
):
    from ledgerops import backup

    original = backup._sync
    destination = tmp_path / "backup"

    def fail_after_rename(directory):
        if directory == tmp_path and destination.exists():
            raise OSError("publication durability unknown")
        original(directory)

    monkeypatch.setattr(backup, "_sync", fail_after_rename)
    with seeded_db.connect("admin") as conn:
        with pytest.raises(OSError, match="publication durability unknown"):
            create_backup(conn, pg_tools, ObjectStore(tmp_path / "objects"), destination)
    assert inspect_backup(destination)["backup_version"] == 1
    assert not list(tmp_path.glob(".ledgerops-backup-*"))
